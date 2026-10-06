"""Tests TLS, observation et arret du profil de trafic."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ml import traffic_test as traffic


LOGIN = b'<html><form><input name="username"><input name="password" type="password"></form></html>'


def snapshot(**metrics):
    values = {
        "cpu_usage_percent": 10.0, "memory_usage_percent": 26.0,
        "disk_available_percent": 40.0, "moodle_available": True,
        "moodle_probe_duration_ms": 75.0, "moodle_http_status_code": 200,
    }
    values.update(metrics)
    return {
        "evaluated_at": traffic.utc_iso(time.time()), "source": "prometheus",
        "collection": {name: {"status": "up", "scrape_age_seconds": 1.0} for name in ("linux", "moodle_https")},
        "metrics": values,
    }


class FixtureReader:
    url = "http://127.0.0.1:8000/api/v1/metrics/summary"

    def __init__(self, factory=snapshot):
        self.factory = factory
        self.calls = 0

    def read(self):
        self.calls += 1
        return self.factory()


class FixtureClient:
    target = traffic.Target("moodle-pfe.test", 8443, "/login/index.php", "127.0.0.1")
    timeout = 5

    def __init__(self, delay=0, fail_traffic=False):
        self.delay, self.fail_traffic = delay, fail_traffic
        self.active = self.peak = 0
        self.lock = threading.Lock()

    def request(self, request_id, phase):
        started, at = time.monotonic(), traffic.utc_iso(time.time())
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(self.delay)
        with self.lock:
            self.active -= 1
        failed = self.fail_traffic and phase != "preflight"
        return traffic.Outcome(
            request_id, phase, at, traffic.utc_iso(time.time()), 500 if failed else 200,
            int(not failed), (time.monotonic() - started) * 1000, len(LOGIN),
            "unexpected_http_status" if failed else None,
        )


@contextmanager
def tls_server(context, *, status=200, body=LOGIN, content_type="text/html", delay=0,
               declared_length=None, chunked=False):
    requests, names = [], []
    context.set_servername_callback(lambda connection, name, ssl_context: names.append(name))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            requests.append((self.command, self.path, self.headers.get("Host")))
            time.sleep(delay)
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Connection", "close")
                if chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.send_header("Content-Length", str(len(body) if declared_length is None else declared_length))
                self.end_headers()
                wire = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n" if chunked else body
                self.wfile.write(wire)
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        yield server.server_port, requests, names
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class TLSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("openssl"):
            raise unittest.SkipTest("OpenSSL requis pour les certificats temporaires de test.")
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        cls.cert, cls.key = root / "server.crt", root / "server.key"
        cls.other_cert = root / "other.crt"
        for hostname, cert, key in (
            ("moodle-pfe.test", cls.cert, cls.key),
            ("other.test", cls.other_cert, root / "other.key"),
        ):
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                "-subj", f"/CN={hostname}", "-addext", f"subjectAltName=DNS:{hostname}",
                "-keyout", str(key), "-out", str(cert),
            ], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def server_context(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        return context

    def client(self, port, *, hostname="moodle-pfe.test", ca=None, **options):
        context = ssl.create_default_context(cafile=str(ca or self.cert))
        target = traffic.target_from_url(f"https://{hostname}:{port}/login/index.php", "127.0.0.1")
        return traffic.LoginClient(target, context, **options)

    def test_loopback_connection_preserves_host_sni_and_certificate_validation(self):
        with tls_server(self.server_context()) as (port, requests, names):
            result = self.client(port).request(1, "preflight")
            self.assertEqual(result.success, 1)
            self.assertEqual(result.http_status, 200)
            self.assertEqual(result.bytes_read, len(LOGIN))
            self.assertEqual(requests, [("GET", "/login/index.php", f"moodle-pfe.test:{port}")])
            self.assertEqual(names, ["moodle-pfe.test"])

    def test_wrong_hostname_and_untrusted_certificate_are_rejected(self):
        with tls_server(self.server_context()) as (port, requests, _):
            wrong_name = self.client(port, hostname="wrong.test").request(1, "preflight")
            wrong_ca = self.client(port, ca=self.other_cert).request(2, "preflight")
            self.assertEqual(wrong_name.error, "tls_verification_failed")
            self.assertEqual(wrong_ca.error, "tls_verification_failed")
            self.assertEqual(requests, [])

    def test_redirects_are_reported_and_not_followed(self):
        with tls_server(self.server_context(), status=302) as (port, requests, _):
            result = self.client(port).request(1, "traffic")
            self.assertEqual(result.error, "unexpected_http_status")
            self.assertEqual(result.http_status, 302)
            self.assertEqual(len(requests), 1)

    def test_http_200_without_the_login_form_is_not_a_success(self):
        with tls_server(self.server_context(), body=b"<html>Erreur Moodle</html>") as (port, _, _):
            self.assertEqual(self.client(port).request(1, "traffic").error, "login_form_missing")

    def test_response_size_and_network_deadline_are_enforced(self):
        with tls_server(self.server_context()) as (port, _, _):
            result = self.client(port, max_body=20).request(1, "traffic")
            self.assertEqual(result.error, "response_too_large")
        with tls_server(self.server_context(), delay=0.15) as (port, _, _):
            result = self.client(port, timeout=0.05).request(1, "traffic")
            self.assertEqual(result.error, "timeout")
            self.assertLess(result.latency_ms, 300)

    def test_incomplete_body_is_rejected_and_chunked_body_is_supported(self):
        with tls_server(self.server_context(), declared_length=len(LOGIN) + 100) as (port, _, _):
            self.assertEqual(self.client(port).request(1, "traffic").error, "incomplete_response")
        with tls_server(self.server_context(), chunked=True) as (port, _, _):
            self.assertEqual(self.client(port).request(1, "traffic").success, 1)

    def test_complete_run_records_real_tls_requests_and_artifact_checksums(self):
        with tempfile.TemporaryDirectory() as temporary, tls_server(self.server_context()) as (port, requests, _):
            run = traffic.TrafficRun(self.client(port), FixtureReader(), [
                traffic.Phase("traffic", 0.35, 4), traffic.Phase("recovery", 0.02, 0),
            ], Path(temporary), guard_interval=0.05)
            report, directory = run.execute()
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["requests"]["failed_requests"], 0)
            self.assertGreaterEqual(len(requests), 2)
            self.assertEqual(report["requests"]["completed_requests"], len(requests))
            for name, checksum in report["artifacts"].items():
                self.assertEqual(checksum, traffic.file_sha256(directory / name))
            self.assertEqual(json.loads((directory / "report.json").read_text()), report)


class GuardTests(unittest.TestCase):
    def test_normal_metrics_and_zero_cpu_are_accepted(self):
        self.assertIsNone(traffic.Guard().evaluate(snapshot(cpu_usage_percent=0)))

    def test_stale_missing_and_failed_probe_stop_traffic(self):
        cases = []
        stale = snapshot()
        stale["collection"]["linux"]["scrape_age_seconds"] = 61
        cases.append((stale, "metrics_collection_unavailable"))
        cases.extend([
            (snapshot(cpu_usage_percent=None), "metrics_missing_or_invalid"),
            (snapshot(moodle_available=False), "moodle_probe_failed"),
            (snapshot(memory_usage_percent=85), "memory_limit_reached"),
            (snapshot(disk_available_percent=10), "disk_space_low"),
        ])
        for values, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(traffic.Guard().evaluate(values), reason)

    def test_cpu_and_latency_require_sustained_observations_and_reset(self):
        guard = traffic.Guard()
        self.assertIsNone(guard.evaluate(snapshot(cpu_usage_percent=95)))
        self.assertIsNone(guard.evaluate(snapshot(cpu_usage_percent=95)))
        self.assertIsNone(guard.evaluate(snapshot(cpu_usage_percent=10)))
        self.assertIsNone(guard.evaluate(snapshot(cpu_usage_percent=95)))
        self.assertIsNone(guard.evaluate(snapshot(cpu_usage_percent=95)))
        self.assertEqual(guard.evaluate(snapshot(cpu_usage_percent=95)), "cpu_sustained_high")
        guard = traffic.Guard()
        self.assertIsNone(guard.evaluate(snapshot(moodle_probe_duration_ms=2000)))
        self.assertEqual(guard.evaluate(snapshot(moodle_probe_duration_ms=2000)), "moodle_probe_sustained_slow")

    def test_malformed_dates_nested_objects_and_non_finite_metrics_are_rejected(self):
        for values in (None, {"source": "prometheus", "evaluated_at": None}, snapshot(cpu_usage_percent=float("nan"))):
            with self.subTest(values=values):
                self.assertEqual(traffic.Guard().evaluate(values), "metrics_missing_or_invalid")
        values = snapshot()
        values["evaluated_at"] = traffic.utc_iso(time.time() - 61)
        self.assertEqual(traffic.Guard().evaluate(values), "metrics_snapshot_stale")

    def test_metrics_reader_rejects_non_standard_json_and_network_errors(self):
        reader = traffic.MetricsReader(FixtureReader.url)
        with patch.object(reader.opener, "open", return_value=io.BytesIO(b'{"cpu":NaN}')):
            with self.assertRaises(traffic.RunError):
                reader.read()
        with patch.object(reader.opener, "open", side_effect=TimeoutError):
            with self.assertRaises(traffic.RunError):
                reader.read()


class RunnerTests(unittest.TestCase):
    def test_unavailable_observation_blocks_target_requests(self):
        def unavailable():
            raise traffic.RunError("indisponible")
        with tempfile.TemporaryDirectory() as temporary:
            report, _ = traffic.TrafficRun(FixtureClient(), FixtureReader(unavailable), [
                traffic.Phase("recovery", 0.01, 0),
            ], Path(temporary)).execute()
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["requests"]["completed_requests"], 0)

    def test_sustained_cpu_stops_before_load_and_still_observes_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, _ = traffic.TrafficRun(FixtureClient(), FixtureReader(lambda: snapshot(cpu_usage_percent=95)), [
                traffic.Phase("baseline", 0.15, 0), traffic.Phase("traffic", 0.1, 4),
                traffic.Phase("recovery", 0.05, 0),
            ], Path(temporary), guard_interval=0.01).execute()
            self.assertEqual(report["status"], "aborted")
            self.assertEqual(report["stop_reason"], "cpu_sustained_high")
            self.assertEqual(report["requests"]["completed_requests"], 1)
            self.assertEqual([phase["name"] for phase in report["phases"]], ["baseline", "recovery"])

    def test_queue_is_bounded_and_busy_offers_are_skipped(self):
        client = FixtureClient(delay=0.35)
        with tempfile.TemporaryDirectory() as temporary:
            report, _ = traffic.TrafficRun(client, FixtureReader(), [
                traffic.Phase("traffic", 0.65, 4), traffic.Phase("recovery", 0.01, 0),
            ], Path(temporary), workers=1, guard_interval=0.05).execute()
            phase = report["phases"][0]
            self.assertEqual(client.peak, 1)
            self.assertGreater(phase["offers_skipped_busy"], 0)
            self.assertLessEqual(phase["completed_requests"], 3)

    def test_repeated_http_failures_stop_traffic(self):
        with tempfile.TemporaryDirectory() as temporary:
            report, _ = traffic.TrafficRun(FixtureClient(fail_traffic=True), FixtureReader(), [
                traffic.Phase("traffic", 1, 4), traffic.Phase("recovery", 0.01, 0),
            ], Path(temporary), guard_interval=0.05).execute()
            self.assertEqual(report["status"], "aborted")
            self.assertEqual(report["stop_reason"], "consecutive_request_failures")
            self.assertEqual(report["requests"]["failed_requests"], 3)

    def test_operator_interrupt_preserves_a_report_and_stops_the_run(self):
        calls = 0
        def interrupt():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise KeyboardInterrupt
            return snapshot()
        with tempfile.TemporaryDirectory() as temporary:
            report, directory = traffic.TrafficRun(FixtureClient(), FixtureReader(interrupt), [
                traffic.Phase("baseline", 1, 0), traffic.Phase("recovery", 1, 0),
            ], Path(temporary), guard_interval=0.01).execute()
            self.assertEqual(report["status"], "interrupted")
            self.assertEqual(report["phases"][0]["status"], "interrupted")
            self.assertTrue((directory / "report.json").exists())

    def test_invalid_observation_still_produces_a_valid_blocked_report(self):
        def invalid():
            values = snapshot()
            values["metrics"] = None
            return values
        with tempfile.TemporaryDirectory() as temporary:
            report, _ = traffic.TrafficRun(FixtureClient(), FixtureReader(invalid), [
                traffic.Phase("recovery", 0.01, 0),
            ], Path(temporary)).execute()
            self.assertEqual(report["status"], "blocked")
            self.assertIsNone(report["observed_maxima"]["cpu_usage_percent"])


class ConfigurationTests(unittest.TestCase):
    def test_only_the_https_login_page_and_loopback_addresses_are_accepted(self):
        for url, ip in (
            ("http://moodle-pfe.test/login/index.php", "127.0.0.1"),
            ("https://moodle-pfe.test/admin/index.php", "127.0.0.1"),
            ("https://moodle-pfe.test/login/index.php", "192.168.1.1"),
            ("https://user:password@moodle-pfe.test/login/index.php", "127.0.0.1"),
        ):
            with self.subTest(url=url), self.assertRaises(traffic.RunError):
                traffic.target_from_url(url, ip)

    def test_insecure_tls_context_and_excessive_load_are_rejected(self):
        with self.assertRaises(traffic.RunError):
            traffic.LoginClient(FixtureClient.target, ssl._create_unverified_context())
        with self.assertRaises(traffic.RunError):
            traffic.TrafficRun(FixtureClient(), FixtureReader(), [
                traffic.Phase("traffic", 1, 5), traffic.Phase("recovery", 1, 0),
            ], Path("unused"))

    def test_profile_duration_and_percentiles_have_explicit_semantics(self):
        self.assertEqual(sum(phase.seconds for phase in traffic.profile("full")), 600)
        self.assertEqual(sum(phase.seconds for phase in traffic.profile("smoke")), 60)
        outcomes = [traffic.Outcome(i, "test", "", "", 200, 1, i, 0, None) for i in range(1, 21)]
        summary = traffic.describe_requests(outcomes)
        self.assertEqual(summary["latency_ms"]["p95"], 19)
        self.assertEqual(summary["latency_ms"]["median"], 10.5)
        self.assertIsNone(traffic.describe_requests([])["latency_ms"]["p95"])


if __name__ == "__main__":
    unittest.main()
