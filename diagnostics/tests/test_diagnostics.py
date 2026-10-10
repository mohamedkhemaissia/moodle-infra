import fcntl
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from diagnostics.collector import history, local_url, get_json, ProbeError
from diagnostics.policy import Blocked, POLICY_SHA, validate
from diagnostics.run import execute
from diagnostics.store import DiagnosticStore, DiagnosticStorageError, strict_json


def recommendation(now=None, incident_id=1):
    from datetime import datetime, timezone
    stamp = int(now or time.time()) // 30 * 30
    date = lambda value: datetime.fromtimestamp(value, timezone.utc).isoformat()
    return {"status": "available", "mode": "simulation", "snapshot_usable": True,
            "policy_sha256": POLICY_SHA, "decision": {
                "incident_id": incident_id, "incident_status": "open", "latest_state": "triggered",
                "status": "recommendation", "policy_version": "1.1.0", "policy_sha256": POLICY_SHA,
                "input_evaluated_at": date(stamp), "valid_until": date(stamp + 90),
                "decision_id": "a" * 64, "context": "model:test",
                "recommended_action": {"id": "wait_contiguous_history", "kind": "diagnostic_recommendation", "executable": False}}}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DiagnosticStore(self.tmp.name)
        self.store.initialize()
    def tearDown(self):
        self.tmp.cleanup()
    def test_persistence_filter_and_readonly_connection(self):
        self.store.start("a" * 32, 1, 1)
        self.store.finish("a" * 32, "completed", 2, report={"checks": {"x": "warning"}}, events=[])
        reader = DiagnosticStore(self.tmp.name)
        self.assertEqual(reader.detail("a" * 32)["report"]["checks"]["x"], "warning")
        self.assertEqual(reader.listing(incident_id=2)["total"], 0)
        self.assertFalse(reader.initialize())
        with reader.connect() as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM runs")
    def test_terminal_status_cannot_be_rewritten(self):
        self.store.start("a" * 32, 1, 1)
        self.store.finish("a" * 32, "blocked", 2)
        with self.assertRaises(DiagnosticStorageError):
            self.store.finish("a" * 32, "completed", 3)
    def test_incompatible_database_is_preserved(self):
        self.store.path.unlink()
        with sqlite3.connect(self.store.path) as connection:
            connection.execute("CREATE TABLE old_data(value TEXT)")
            connection.execute("INSERT INTO old_data VALUES('preserved')")
        before = self.store.path.read_bytes()
        with self.assertRaises(DiagnosticStorageError):
            self.store.initialize()
        self.assertEqual(before, self.store.path.read_bytes())
    def test_malformed_nonfinite_and_oversize_json_rejected(self):
        for body in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '"' + 'x' * 262144 + '"'):
            with self.assertRaises(ValueError):
                strict_json(body)


class PolicyTests(unittest.TestCase):
    def test_current_recommendation_and_changed_context(self):
        document = recommendation(1000)
        expected = validate(document, 1, 1000)
        self.assertEqual(expected["profile"], "collection")
        document["decision"]["context"] = "other:model"
        with self.assertRaises(Blocked):
            validate(document, 1, 1000, expected=expected)
    def test_expiry_unknown_policy_action_and_closed_incident_block(self):
        changes = [("valid_until", "1970-01-01T00:00:00Z"), ("policy_sha256", "b" * 64),
                   ("incident_status", "resolved"), ("latest_state", "unknown")]
        for key, value in changes:
            document = recommendation(1000)
            document["decision"][key] = value
            with self.assertRaises(Blocked):
                validate(document, 1, 1000)
        document = recommendation(1000)
        document["decision"]["recommended_action"]["id"] = "restart_php"
        with self.assertRaises(Blocked):
            validate(document, 1, 1000)
    def test_remote_urls_credentials_and_redirect_destination_rejected(self):
        for url in ("https://127.0.0.1:9090", "http://example.com:9090", "http://user:pass@127.0.0.1:9090", "http://127.0.0.1:9090/path"):
            with self.assertRaises(ValueError):
                local_url(url)


class ProbeTests(unittest.TestCase):
    def test_history_gaps_stale_and_down_are_not_filled(self):
        def response(url):
            query = parse_qs(urlsplit(url).query)["query"][0]
            value = "1" if query.startswith("up{") else "10"
            vals = [[timestamp, value] for timestamp in range(0, 421, 30)]
            if query.startswith("time()"):
                vals[3][1] = "100"
            if query.startswith("up{"):
                vals[4][1] = "0"
            if query.startswith("100 * (1 - avg"):
                vals.pop(5)
            return {"status": "success", "data": {"resultType": "matrix", "result": [{"values": vals}]}}
        with patch("diagnostics.collector.get_json", side_effect=response):
            result = history({"prometheus_url": "http://127.0.0.1:9090"}, 420)
        self.assertEqual(result["status"], "warning")
        self.assertEqual([row["timestamp"] for row in result["invalid_points"]], [90, 120, 150])
        self.assertIsNone(result["invalid_points"][-1]["cpu"])
    def test_ambiguous_series_is_not_aggregated(self):
        body = {"status": "success", "data": {"resultType": "matrix", "result": [{}, {}]}}
        with patch("diagnostics.collector.get_json", return_value=body):
            with self.assertRaises(ProbeError):
                history({"prometheus_url": "http://127.0.0.1:9090"}, 420)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = {"data_dir": self.tmp.name, "api_url": "http://127.0.0.1:8000", "prometheus_url": "http://127.0.0.1:9090"}
        self.store = DiagnosticStore(self.tmp.name)
        self.store.initialize()
    def tearDown(self):
        self.tmp.cleanup()
    def test_expired_preflight_never_starts_runner_and_is_audited(self):
        with patch("diagnostics.run.get_json", return_value=recommendation(1000)), patch("diagnostics.run.time.time", return_value=2000):
            def forbidden(**kwargs):
                self.fail("Runner ne doit pas demarrer.")
            result, code = execute(self.config, Path("unused"), 1, runner_function=forbidden)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(code, 2)
        self.assertEqual(self.store.detail(result["run_id"])["report"]["reason"], "preflight_revalidation_failed")
    def test_success_persists_only_normalized_events_and_report(self):
        decision = recommendation()
        def runner(**kwargs):
            data = {"task": "Collecter les preuves en lecture seule", "host": "local", "res": {
                "stdout": json.dumps({"status": "completed", "checks": {"collection": "warning"},
                                      "decision": validate(decision, 1, time.time())}), "secret": "not_copied"}}
            kwargs["event_handler"]({"event": "runner_on_ok", "event_data": data})
            return SimpleNamespace(rc=0, status="successful")
        with patch("diagnostics.run.get_json", return_value=decision):
            result, code = execute(self.config, Path("unused"), 1, runner_function=runner)
        detail = self.store.detail(result["run_id"])
        self.assertEqual(code, 0)
        self.assertEqual(detail["runner_rc"], 0)
        self.assertNotIn("not_copied", json.dumps(detail))
    def test_interrupt_failure_and_timeout_are_audited(self):
        for status in ("canceled", "timeout", "failed"):
            def runner(**kwargs):
                if status == "canceled":
                    raise KeyboardInterrupt
                return SimpleNamespace(rc=1, status=status)
            with patch("diagnostics.run.get_json", return_value=recommendation()):
                result, code = execute(self.config, Path("unused"), 1, runner_function=runner)
            self.assertEqual(result["status"], status)
            self.assertEqual(self.store.detail(result["run_id"])["status"], status)
            self.assertEqual(code, 2)
    def test_concurrent_worker_is_rejected_without_second_run(self):
        with (Path(self.tmp.name) / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result, code = execute(self.config, Path("unused"), 1)
        self.assertEqual(result["status"], "busy")
        self.assertEqual(self.store.listing()["total"], 0)


class RealRunnerTests(unittest.TestCase):
    def test_real_runner_collects_local_http_proofs_and_creates_persistent_report(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                path = urlsplit(self.path)
                if path.path.startswith("/api/v1/decisions/"):
                    body = recommendation()
                elif path.path == "/api/v1/targets":
                    body = {"status": "success", "data": {"activeTargets": []}}
                elif path.path == "/api/v1/query_range":
                    params = parse_qs(path.query)
                    query = params["query"][0]
                    value = "1" if query.startswith("up{") else "10"
                    vals = [[stamp, value] for stamp in range(int(params["start"][0]), int(params["end"][0]) + 1, 30)]
                    body = {"status": "success", "data": {"resultType": "matrix", "result": [{"values": vals}]}}
                else:
                    self.send_error(404)
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = DiagnosticStore(tmp)
                store.initialize()
                config = {"api_url": "http://127.0.0.1:" + str(server.server_port),
                          "prometheus_url": "http://127.0.0.1:" + str(server.server_port), "data_dir": tmp}
                path = Path(tmp) / "config.json"
                path.write_text(json.dumps(config))
                result, code = execute(config, path, 1)
                detail = store.detail(result["run_id"])
                self.assertEqual((code, result["status"]), (0, "completed"), detail)
                self.assertEqual(detail["runner_status"], "successful")
                self.assertEqual(detail["report"]["checks"]["linux_history"]["valid_points"], 15)
                self.assertEqual(detail["report"]["checks"]["prometheus_targets"]["status"], "warning")
                self.assertTrue(any(e["event"] == "runner_on_ok" for e in detail["events"]))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
