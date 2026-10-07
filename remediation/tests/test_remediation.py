import copy
import json
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from diagnostics.policy import Blocked, POLICY_SHA
from remediation.action import apply, container_gate, operations_clear, config_read
from remediation.policy import decision_for_php, metric_gate
from remediation.probes import login_probe
from remediation.run import execute, CODE_DIR
from remediation.store import RemediationStore


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def decision(incident_id=1, now=None):
    stamp = int(now or time.time()) // 30 * 30
    return {"status": "available", "mode": "simulation", "snapshot_usable": True,
            "policy_sha256": POLICY_SHA, "decision": {
                "incident_id": incident_id, "signal_key": "moodle_https", "incident_status": "open",
                "latest_state": "triggered", "status": "recommendation", "policy_version": "1.0.0",
                "policy_sha256": POLICY_SHA, "input_evaluated_at": iso(stamp), "valid_until": iso(stamp + 90),
                "context": "policy-v1", "decision_id": "a" * 64, "evidence": {"probe_success": False},
                "recommended_action": {"id": "diagnose_moodle_https", "kind": "diagnostic_recommendation", "executable": False}}}


def metrics(available=False, now=None):
    return {"source": "prometheus", "evaluated_at": iso(now or time.time()),
            "collection": {job: {"status": "up", "scrape_age_seconds": 0} for job in ("linux", "moodle_https")},
            "metrics": {"cpu_usage_percent": 10, "memory_usage_percent": 30, "disk_available_percent": 40,
                        "moodle_available": available, "moodle_http_status_code": 200 if available else 503}}


def container(name, running=None):
    if running is None:
        running = name != "php"
    return {"id": {"php": "a", "apache": "b", "postgres": "c"}[name] * 64, "service": name,
            "state": {"Status": "running" if running else "exited", "Running": running,
                      "Restarting": False, "Paused": False, "OOMKilled": False, "Dead": False, "ExitCode": 0},
            "health": "healthy" if name == "postgres" else None, "offline_maintenance": False}


class GuardTests(unittest.TestCase):
    def test_only_a_recent_confirmed_https_incident_is_eligible(self):
        self.assertEqual(decision_for_php(decision(), 1, time.time())["correction_action"], "start_php_stopped")
        for change in ("closed", "wrong_signal", "expired", "wrong_policy"):
            d = decision()
            if change == "closed": d["decision"]["incident_status"] = "resolved"
            if change == "wrong_signal": d["decision"]["signal_key"] = "analysis_history"
            if change == "expired": d["decision"]["valid_until"] = iso(1)
            if change == "wrong_policy": d["policy_sha256"] = "b" * 64
            with self.assertRaises(Blocked): decision_for_php(d, 1, time.time())
    def test_stale_unknown_healthy_and_pressure_metrics_block(self):
        for change in ("stale", "unknown", "already_up", "cpu", "memory", "disk", "nan"):
            m = metrics()
            if change == "stale": m["collection"]["linux"]["scrape_age_seconds"] = 70
            if change == "unknown": m["metrics"]["moodle_available"] = None
            if change == "already_up": m["metrics"]["moodle_available"] = True
            if change == "cpu": m["metrics"]["cpu_usage_percent"] = 85
            if change == "memory": m["metrics"]["memory_usage_percent"] = 90
            if change == "disk": m["metrics"]["disk_available_percent"] = 10
            if change == "nan": m["metrics"]["cpu_usage_percent"] = float("nan")
            with self.assertRaises(Blocked): metric_gate(m, time.time(), available=False)
    def test_running_oom_maintenance_or_unhealthy_dependency_blocks(self):
        for change in ("running", "oom", "maintenance", "db", "apache"):
            states = {name: container(name) for name in ("php", "apache", "postgres")}
            if change == "running": states["php"] = container("php", True)
            if change == "oom": states["php"]["state"]["OOMKilled"] = True
            if change == "maintenance": states["php"]["offline_maintenance"] = True
            if change == "db": states["postgres"]["health"] = "unhealthy"
            if change == "apache": states["apache"] = container("apache", False)
            with self.assertRaises(Blocked): container_gate(states)
    def test_restore_marker_and_backup_systemd_service_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "active"
            config = {"restoration_marker": str(marker)}
            marker.touch()
            with self.assertRaises(Blocked): operations_clear(config)
            marker.unlink()
            output = SimpleNamespace(returncode=0, stdout='[{"unit":"moodle-backup.service"}]')
            with patch("remediation.action.command", return_value=output):
                with self.assertRaises(Blocked): operations_clear(config)


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RemediationStore(self.tmp.name); self.store.initialize()
        self.identifier = "d" * 32; self.store.start(self.identifier, 1, time.time())
        self.config = {"api_url": "http://127.0.0.1:8000", "data_dir": self.tmp.name}
        self.expected = decision_for_php(decision(), 1, time.time())
        self.started = False
        self.calls = []
    def tearDown(self):
        self.tmp.cleanup()
    def json(self, url):
        return decision() if "/decisions/" in url else metrics(self.started)
    def inspect(self, name):
        return container(name, True if name == "php" and self.started else None)
    def probe(self):
        return {"success": self.started, "tls_validated": True, "http_status": 200 if self.started else 503}
    def start(self, identifier):
        # L'intention est deja durable lorsque Docker recoit sa premiere commande.
        self.assertIsNotNone(self.store.detail(self.identifier)["action_reservation"])
        self.calls.append(identifier); self.started = True
        return SimpleNamespace(returncode=0)
    def run_action(self, **kwargs):
        defaults = dict(inspect_fn=self.inspect, probe_fn=self.probe, operations_fn=lambda config: None,
                        start_fn=self.start, pause=lambda seconds: None, wait_seconds=5)
        defaults.update(kwargs)
        with patch("remediation.action.get_json", side_effect=self.json):
            return apply(self.config, 1, self.identifier, self.expected, **defaults)
    def test_action_once_and_three_https_checks_with_new_prometheus_sample(self):
        result = self.run_action()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["recovered"])
        self.assertEqual(self.calls, ["a" * 64])
        self.assertEqual(len(result["checks"]["recovery_samples"]), 3)
    def test_a_tls_error_never_dispatches_the_action(self):
        result = self.run_action(probe_fn=lambda: {"success": False, "tls_validated": False, "http_status": None})
        self.assertEqual(result["status"], "blocked"); self.assertEqual(self.calls, [])
    def test_http_500_with_confirmed_stopped_php_requires_proven_recovery(self):
        def probe():
            return {"success": self.started, "tls_validated": True,
                    "http_status": 200 if self.started else 500}
        def json_read(url):
            if "/decisions/" in url:
                return decision()
            result = metrics(self.started)
            result["metrics"]["moodle_http_status_code"] = 200 if self.started else 500
            return result
        with patch("remediation.action.get_json", side_effect=json_read):
            result = apply(self.config, 1, self.identifier, self.expected,
                           inspect_fn=self.inspect, probe_fn=probe,
                           operations_fn=lambda config: None, start_fn=self.start,
                           pause=lambda seconds: None, wait_seconds=5)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["recovered"])
        self.assertEqual(result["checks"]["https_before"]["http_status"], 500)
        self.assertEqual(self.calls, ["a" * 64])
        self.assertEqual(len(result["checks"]["recovery_samples"]), 3)
        self.assertIsNotNone(self.store.detail(self.identifier)["action_reservation"])
    def test_http_500_with_running_php_never_dispatches(self):
        result = self.run_action(inspect_fn=lambda name: container(name, True),
                                probe_fn=lambda: {"success": False, "tls_validated": True, "http_status": 500})
        self.assertEqual(result["reason"], "php_not_stopped_or_maintenance")
        self.assertFalse(result["action_dispatched"])
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.store.detail(self.identifier)["action_reservation"])
    def test_http_500_with_unhealthy_database_never_dispatches(self):
        def inspect(name):
            state = container(name)
            if name == "postgres":
                state["health"] = "unhealthy"
            return state
        result = self.run_action(inspect_fn=inspect,
                                probe_fn=lambda: {"success": False, "tls_validated": True, "http_status": 500})
        self.assertEqual(result["reason"], "dependency_not_ready:postgres")
        self.assertFalse(result["action_dispatched"])
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.store.detail(self.identifier)["action_reservation"])
    def test_http_500_without_validated_tls_never_dispatches(self):
        result = self.run_action(probe_fn=lambda: {"success": False, "tls_validated": False, "http_status": 500})
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.store.detail(self.identifier)["action_reservation"])
    def test_other_http_codes_do_not_authorize_stopped_php_start(self):
        for status in (200, 302, 400, 404, 501):
            with self.subTest(http_status=status):
                result = self.run_action(probe_fn=lambda: {
                    "success": False, "tls_validated": True, "http_status": status})
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(self.calls, [])
                self.assertIsNone(self.store.detail(self.identifier)["action_reservation"])
    def test_running_php_never_restarts(self):
        result = self.run_action(inspect_fn=lambda name: container(name, True))
        self.assertEqual(result["status"], "blocked"); self.assertEqual(self.calls, [])
    def test_identity_change_before_dispatch_blocks(self):
        count = 0
        def inspect(name):
            nonlocal count
            count += 1
            result = container(name)
            if count > 3 and name == "php": result["id"] = "f" * 64
            return result
        result = self.run_action(inspect_fn=inspect)
        self.assertEqual(result["reason"], "container_replaced_during_checks"); self.assertEqual(self.calls, [])
    def test_failed_start_reserves_the_attempt_without_claiming_recovery(self):
        result = self.run_action(start_fn=lambda identifier: SimpleNamespace(returncode=1))
        self.assertEqual(result["status"], "failed"); self.assertFalse(result["recovered"])
        self.assertTrue(result["action_dispatched"])
        self.assertIsNotNone(self.store.detail(self.identifier)["action_reservation"])
    def test_old_prometheus_success_is_not_a_recovery_confirmation(self):
        def json_read(url):
            if "/decisions/" in url: return decision()
            m = metrics(self.started)
            if self.started: m["collection"]["moodle_https"]["scrape_age_seconds"] = 30
            return m
        with patch("remediation.action.get_json", side_effect=json_read):
            result = apply(self.config, 1, self.identifier, self.expected, inspect_fn=self.inspect,
                           probe_fn=self.probe, operations_fn=lambda c: None, start_fn=self.start, pause=lambda s: None, wait_seconds=1)
        self.assertEqual(result["status"], "failed"); self.assertFalse(result["recovered"])
    def test_reservation_survives_reopening_and_prevents_replay_and_burst(self):
        self.assertIsNone(self.store.reserve(self.identifier, 1, time.time(), "a" * 64))
        reopened = RemediationStore(self.tmp.name)
        reopened.start("e" * 32, 1, time.time())
        self.assertEqual(reopened.reserve("e" * 32, 1, time.time(), "a" * 64), "already_attempted_for_incident")
        reopened.start("f" * 32, 2, time.time())
        self.assertEqual(reopened.reserve("f" * 32, 2, time.time(), "a" * 64), "cooldown_or_clock_inconsistent")
        self.assertEqual(reopened.detail(self.identifier)["action_reservation"]["run_id"], self.identifier)
    def test_worker_blocks_wrong_incident_before_runner(self):
        d = decision(); d["decision"]["signal_key"] = "analysis_history"
        with patch("remediation.run.get_json", return_value=d):
            def forbidden(**kwargs): self.fail("Runner ne doit pas partir")
            result, code = execute(self.config, Path("unused"), 1, runner_function=forbidden)
        self.assertEqual((result["status"], code), ("blocked", 2))

    def test_worker_interrupt_after_reservation_preserves_intent_and_audit(self):
        def runner(**kwargs):
            identifier = kwargs['extravars']['remediation_run_id']
            self.store.reserve(identifier, 1, time.time(), 'a' * 64)
            raise KeyboardInterrupt
        with patch('remediation.run.get_json', return_value=decision()):
            result, code = execute(self.config, Path('unused'), 1, runner_function=runner)
        self.assertEqual((result['status'], code), ('canceled', 2))
        item = self.store.detail(result['run_id'])
        self.assertIsNotNone(item['action_reservation'])
        self.assertEqual(item['report']['reason'], 'operator_interrupt')


class TLSFixture:
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(); cls.root = Path(cls.tmp.name)
        cls.cert, cls.key = cls.root / "ca.crt", cls.root / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=moodle-pfe.test", "-addext", "subjectAltName=DNS:moodle-pfe.test",
                        "-addext", "basicConstraints=critical,CA:TRUE", "-keyout", str(cls.key), "-out", str(cls.cert)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()
    def server(self, state):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                state["host"] = self.headers.get("Host")
                body = state.get("body", b'<input name="username"><input name="password">')
                self.send_response(state.get("status", 200)); self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(self.cert, self.key)
        context.set_servername_callback(lambda socket, name, context: state.update(sni=name))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(thread.join, 3); self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        return server
class TLSTests(TLSFixture, unittest.TestCase):
    def test_loopback_tls_preserves_sni_host_and_validates_real_login_fields(self):
        state = {}; server = self.server(state)
        self.assertTrue(login_probe("moodle-pfe.test", server.server_port, self.cert)["success"])
        self.assertEqual(state["sni"], "moodle-pfe.test"); self.assertTrue(state["host"].startswith("moodle-pfe.test:"))
    def test_wrong_hostname_redirect_and_missing_form_are_rejected(self):
        state = {}; server = self.server(state)
        self.assertEqual(login_probe("wrong.test", server.server_port, self.cert)["error"], "tls_verification_failed")
        state["status"] = 302
        self.assertFalse(login_probe("moodle-pfe.test", server.server_port, self.cert)["success"])
        state.update(status=200, body=b"<h1>Not Moodle</h1>")
        self.assertEqual(login_probe("moodle-pfe.test", server.server_port, self.cert)["error"], "login_form_missing")


class RealRunnerTests(TLSFixture, unittest.TestCase):
    def test_actual_runner_playbook_action_audit_and_tls_recovery(self):
        self.runner_case(503)

    def test_actual_runner_recovers_http_500_with_stopped_php(self):
        self.runner_case(500)

    def runner_case(self, failure_status):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp); code = directory / "code"; code.mkdir()
            for package in ("remediation", "diagnostics"):
                shutil.copytree(CODE_DIR / package, code / package, ignore=shutil.ignore_patterns('__pycache__'))
            flag = directory / "started"
            state = {}
            class LoginHandler(BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_GET(self):
                    ready = flag.exists(); body = b'<input name="username"><input name="password">' if ready else b'PHP stopped'
                    self.send_response(200 if ready else failure_status); self.send_header('Content-Type', 'text/html')
                    self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
            tls = ThreadingHTTPServer(('127.0.0.1', 0), LoginHandler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(self.cert, self.key)
            tls.socket = context.wrap_socket(tls.socket, server_side=True)
            tls_thread = threading.Thread(target=tls.serve_forever, daemon=True); tls_thread.start()
            class APIHandler(BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_GET(self):
                    body = decision() if '/decisions/' in self.path else metrics(flag.exists())
                    if '/decisions/' not in self.path and not flag.exists():
                        body['metrics']['moodle_http_status_code'] = failure_status
                    data = json.dumps(body).encode(); self.send_response(200); self.send_header('Content-Length',str(len(data)))
                    self.end_headers(); self.wfile.write(data)
            api = ThreadingHTTPServer(('127.0.0.1', 0), APIHandler)
            api_thread = threading.Thread(target=api.serve_forever, daemon=True); api_thread.start()
            # Adaptateurs du banc d'essai uniquement : IPC TCP et doubles Docker/systemd.
            adapter = '''import multiprocessing.connection
multiprocessing.connection.default_family = 'AF_INET'
import subprocess, json
from pathlib import Path
_original = subprocess.run
_flag = Path(FLAG)
def _run(argv, *args, **kwargs):
    if argv[0] == '/usr/bin/systemctl':
        return subprocess.CompletedProcess(argv, 0, '[]', '')
    if argv[0] == '/usr/bin/docker':
        if argv[1] == 'start':
            assert argv[-1] == 'a' * 64
            _flag.touch()
            return subprocess.CompletedProcess(argv, 0, '', '')
        assert argv[1] == 'inspect'
        name = argv[-1].split('-')[-2]
        running = name != 'php' or _flag.exists()
        state = {'Status':'running' if running else 'exited','Running':running,'Restarting':False,'Paused':False,'OOMKilled':False,'Dead':False,'ExitCode':0}
        if name == 'postgres': state['Health'] = {'Status':'healthy'}
        raw = {'id': {'php':'a','apache':'b','postgres':'c'}[name] * 64, 'state':state, 'mounts':[], 'labels':{'com.docker.compose.project':'moodle-pilote','com.docker.compose.service':name,'com.docker.compose.oneoff':'False'}}
        return subprocess.CompletedProcess(argv, 0, json.dumps(raw), '')
    return _original(argv, *args, **kwargs)
subprocess.run = _run
'''.replace('FLAG', repr(str(flag)))
            (code / 'sitecustomize.py').write_text(adapter)
            config = {'api_url':'http://127.0.0.1:' + str(api.server_port), 'data_dir':str(directory),
                      'hostname':'moodle-pfe.test', 'https_port':tls.server_port, 'ca_file':str(self.cert),
                      'restoration_marker':str(directory / 'restoration-active')}
            config_path = directory / 'config.json'; config_path.write_text(json.dumps(config))
            store = RemediationStore(directory); store.initialize()
            try:
                with patch('remediation.run.CODE_DIR', code):
                    result, rc = execute(config, config_path, 1)
                report = store.detail(result['run_id'])
                self.assertEqual((rc, result['status']), (0, 'completed'), report)
                self.assertTrue(flag.exists()); self.assertTrue(report['report']['recovered'])
                self.assertEqual(report['report']['checks']['https_before']['http_status'], failure_status)
                self.assertTrue(report['report']['checks']['https_before']['tls_validated'])
                self.assertIsNotNone(report['action_reservation']); self.assertEqual(report['runner_rc'], 0)
            finally:
                for server, thread in ((tls,tls_thread),(api,api_thread)):
                    server.shutdown(); server.server_close(); thread.join(3)


if __name__ == '__main__': unittest.main()
