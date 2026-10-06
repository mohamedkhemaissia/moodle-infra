"""Verification de la qualite des donnees et des fichiers d'export."""

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from ml import export_metrics as exporter


WINDOW = exporter.Window(1_700_000_010, 1_700_000_070, 30)


def matrix(values, labels=None):
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": labels or {}, "values": values}],
        },
    }


def sample_series():
    defaults = {
        "cpu_usage_percent": 0.0,
        "memory_usage_percent": 25.0,
        "disk_available_percent": 40.0,
        "moodle_available": 1.0,
        "moodle_probe_duration_ms": 75.0,
        "moodle_http_status_code": 200.0,
        "linux_up": 1.0,
        "linux_age": 5.0,
        "moodle_https_up": 1.0,
        "moodle_https_age": 10.0,
    }
    return {
        name: exporter.Series({index: value for index in range(WINDOW.count)}, {})
        for name, value in defaults.items()
    }


class FakeReader:
    def __init__(self, url="http://127.0.0.1:9090", series=None):
        self.url = url
        self.series = sample_series() if series is None else series
        self.calls = []

    def query(self, expression, window):
        self.calls.append((expression, window))
        name = next(name for name, query in exporter.QUERIES.items() if query == expression)
        return self.series[name]


@contextmanager
def prometheus_server(responder):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            parameters = parse_qs(parsed.query)
            requests.append((parsed.path, parameters))
            status, payload = responder(len(requests), parameters)
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class WindowTests(unittest.TestCase):
    def test_default_window_is_six_hours_on_a_utc_grid(self):
        window = exporter.make_window(now=1_800_000_017)
        self.assertEqual(window.count, 721)
        self.assertEqual(window.end - window.start, 6 * 3600)
        self.assertEqual(window.start % 30, 0)
        self.assertEqual(window.end, 1_800_000_000)

    def test_explicit_bounds_are_aligned_inward(self):
        window = exporter.make_window(start=101, end=179, step=30, now=200)
        self.assertEqual(window, exporter.Window(120, 150, 30))

    def test_timestamp_requires_a_timezone_and_normalizes_offsets(self):
        self.assertEqual(
            exporter.parse_timestamp("2026-10-06T15:00:00+01:00"),
            exporter.parse_timestamp("2026-10-06T14:00:00Z"),
        )
        for value in ("2026-10-06T14:00:00", "invalide"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                exporter.parse_timestamp(value)

    def test_invalid_or_excessive_windows_fail_before_querying(self):
        cases = (
            {"hours": 0}, {"hours": float("nan")}, {"hours": 1e308},
            {"hours": 100}, {"step": 10}, {"end": 1_800_000_100},
            {"start": 1_800_000_100}, {"hours": 1, "start": 1_700_000_000},
        )
        for case in cases:
            with self.subTest(case=case), self.assertRaises(exporter.ExportError):
                exporter.make_window(now=1_800_000_000, **case)


class DataQualityTests(unittest.TestCase):
    def test_sparse_matrix_preserves_gaps_and_records_non_finite_values(self):
        payload = matrix([
            [WINDOW.start, "0"], [WINDOW.end, "NaN"],
        ], {"instance": "127.0.0.1:9100"})
        series = exporter.parse_matrix(payload, WINDOW)
        self.assertEqual(series.values, {0: 0.0, 2: None})
        self.assertEqual(series.non_finite_samples, 1)
        self.assertEqual(series.labels["instance"], "127.0.0.1:9100")
        payload["data"]["result"][0]["values"][-1][1] = "+Inf"
        self.assertIsNone(exporter.parse_matrix(payload, WINDOW).values[2])

    def test_empty_matrix_is_a_missing_series(self):
        payload = {"status": "success", "data": {"resultType": "matrix", "result": []}}
        self.assertEqual(exporter.parse_matrix(payload, WINDOW).values, {})

    def test_ambiguous_or_malformed_samples_are_rejected(self):
        multiple = matrix([[WINDOW.start, "1"]])
        multiple["data"]["result"].append({"metric": {}, "values": []})
        warning = matrix([[WINDOW.start, "1"]])
        warning["warnings"] = ["partial data"]
        payloads = (
            multiple, warning, {"status": "error"},
            matrix([[WINDOW.start + 1, "1"]]),
            matrix([[WINDOW.start, "1"], [WINDOW.start, "2"]]),
            matrix([[WINDOW.end, "1"], [WINDOW.start, "2"]]),
            matrix([[WINDOW.end + WINDOW.step, "1"]]),
            matrix([[WINDOW.start, "invalide"]]),
        )
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(exporter.ExportError):
                exporter.parse_matrix(payload, WINDOW)

    def test_zero_and_failed_probe_are_valid_observations(self):
        series = sample_series()
        series["moodle_available"].values[1] = 0.0
        series["moodle_http_status_code"].values[1] = 0.0
        rows = exporter.build_rows(series, WINDOW)
        self.assertEqual(rows[1]["cpu_usage_percent"], 0.0)
        self.assertEqual(rows[1]["moodle_available"], 0)
        self.assertEqual(rows[1]["moodle_http_status_code"], 0)
        self.assertEqual(rows[1]["all_metrics_complete"], 1)
        self.assertEqual(exporter.summarize(rows)["failed_probe_rows"], 1)

    def test_sources_are_filtered_independently_for_down_and_stale_collection(self):
        series = sample_series()
        series["linux_up"].values[1] = 0.0
        series["moodle_https_age"].values[2] = 61.0
        rows = exporter.build_rows(series, WINDOW)
        self.assertEqual(rows[1]["linux_collection_status"], "down")
        self.assertIsNone(rows[1]["cpu_usage_percent"])
        self.assertEqual(rows[1]["moodle_available"], 1)
        self.assertEqual(rows[2]["moodle_https_collection_status"], "stale")
        self.assertIsNone(rows[2]["moodle_available"])
        self.assertEqual(rows[2]["resource_metrics_complete"], 1)
        self.assertEqual(rows[2]["all_metrics_complete"], 0)

    def test_missing_values_remain_unknown_and_impossible_values_are_excluded(self):
        series = sample_series()
        del series["linux_age"].values[0]
        del series["moodle_available"].values[1]
        series["memory_usage_percent"].values[2] = 101.0
        series["moodle_http_status_code"].values[2] = 200.5
        rows = exporter.build_rows(series, WINDOW)
        self.assertEqual(rows[0]["linux_collection_status"], "missing")
        self.assertIsNone(rows[0]["cpu_usage_percent"])
        self.assertIsNone(rows[1]["moodle_available"])
        self.assertIsNone(rows[2]["memory_usage_percent"])
        self.assertIsNone(rows[2]["moodle_http_status_code"])
        self.assertEqual(exporter.summarize(rows)["unknown_probe_rows"], 1)


class ExportTests(unittest.TestCase):
    def test_csv_and_metadata_match_and_repeated_export_is_reused(self):
        series = sample_series()
        del series["cpu_usage_percent"].values[1]
        reader = FakeReader(series=series)
        with tempfile.TemporaryDirectory() as temporary:
            metadata, directory, reused = exporter.perform_export(reader, WINDOW, Path(temporary))
            self.assertFalse(reused)
            self.assertEqual(len(reader.calls), len(exporter.QUERIES))
            with (directory / "metrics.csv").open(newline="") as stream:
                csv_reader = csv.DictReader(stream)
                self.assertEqual(csv_reader.fieldnames, list(exporter.CSV_FIELDS))
                rows = list(csv_reader)
            self.assertEqual(len(rows), WINDOW.count)
            self.assertEqual(rows[0]["cpu_usage_percent"], "0.0")
            self.assertEqual(rows[1]["cpu_usage_percent"], "")
            self.assertEqual(rows[-1]["evaluated_at"], exporter.utc_iso(WINDOW.end))
            self.assertEqual(metadata["csv_sha256"], exporter.file_sha256(directory / "metrics.csv"))
            self.assertEqual(metadata["summary"]["resource_complete_rows"], 2)
            stored = json.loads((directory / "metadata.json").read_text())
            self.assertEqual(stored, metadata)
            again, same_directory, reused = exporter.perform_export(reader, WINDOW, Path(temporary))
            self.assertTrue(reused)
            self.assertEqual(again, metadata)
            self.assertEqual(same_directory, directory)
            self.assertEqual(len(reader.calls), len(exporter.QUERIES))
            self.assertEqual(list(Path(temporary).iterdir()), [directory])

    def test_corrupted_existing_export_is_rejected_without_overwriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            reader = FakeReader()
            _, directory, _ = exporter.perform_export(reader, WINDOW, Path(temporary))
            path = directory / "metrics.csv"
            path.write_text("corrompu\n")
            with self.assertRaises(exporter.ExportError):
                exporter.perform_export(reader, WINDOW, Path(temporary))
            self.assertEqual(path.read_text(), "corrompu\n")
            self.assertEqual(len(reader.calls), len(exporter.QUERIES))

    def test_different_freshness_policy_has_its_own_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = exporter.perform_export(FakeReader(), WINDOW, Path(temporary), max_age=60)
            second = exporter.perform_export(FakeReader(), WINDOW, Path(temporary), max_age=90)
            self.assertNotEqual(first[1], second[1])

    def test_http_queries_share_the_same_inclusive_range(self):
        def responder(number, parameters):
            return 200, matrix([[WINDOW.start, "1"], [WINDOW.end, "1"]])

        with tempfile.TemporaryDirectory() as temporary, prometheus_server(responder) as (url, requests):
            metadata, _, _ = exporter.perform_export(
                exporter.PrometheusReader(url), WINDOW, Path(temporary)
            )
            self.assertEqual(len(requests), len(exporter.QUERIES))
            for (path, parameters), expression in zip(requests, exporter.QUERIES.values()):
                self.assertEqual(path, "/api/v1/query_range")
                self.assertEqual(parameters["query"], [expression])
                self.assertEqual(parameters["start"], [str(WINDOW.start)])
                self.assertEqual(parameters["end"], [str(WINDOW.end)])
                self.assertEqual(parameters["step"], ["30"])
            self.assertEqual(metadata["summary"]["observed_rows"], 2)

    def test_http_failure_never_publishes_a_partial_export(self):
        def responder(number, parameters):
            if number == 4:
                return 503, {"status": "error"}
            return 200, matrix([[WINDOW.start, "1"]])

        with tempfile.TemporaryDirectory() as temporary, prometheus_server(responder) as (url, requests):
            with self.assertRaisesRegex(exporter.ExportError, "HTTP 503"):
                exporter.perform_export(exporter.PrometheusReader(url), WINDOW, Path(temporary))
            self.assertEqual(len(requests), 4)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_invalid_json_is_reported_as_an_export_error(self):
        with prometheus_server(lambda number, parameters: (200, b"pas du JSON")) as (url, _):
            with self.assertRaisesRegex(exporter.ExportError, "JSON"):
                exporter.PrometheusReader(url).query("up", WINDOW)

    def test_unavailable_prometheus_is_reported_as_an_export_error(self):
        reader = exporter.PrometheusReader("http://127.0.0.1:9090")
        with patch.object(reader.opener, "open", side_effect=TimeoutError):
            with self.assertRaisesRegex(exporter.ExportError, "inaccessible"):
                reader.query("up", WINDOW)

    def test_empty_export_is_saved_but_cli_reports_no_usable_data(self):
        empty = {name: exporter.Series({}, {}) for name in exporter.QUERIES}
        reader = FakeReader(series=empty)
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(exporter, "PrometheusReader", return_value=reader), \
                redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = exporter.main([
                "--start", exporter.utc_iso(WINDOW.start),
                "--end", exporter.utc_iso(WINDOW.end), "--output-dir", temporary,
            ])
            report = json.loads(stdout.getvalue())
            self.assertEqual(code, 3)
            self.assertEqual(report["status"], "no_usable_resource_data")
            self.assertEqual(report["grid_rows"], 3)
            self.assertEqual(report["observed_rows"], 0)
            self.assertTrue(Path(report["csv_file"]).exists())

    def test_invalid_urls_and_invalid_freshness_are_rejected(self):
        for url in ("file:///tmp/prom", "http://user:pass@localhost", "http://localhost:abc", "http://[", "http://localhost?query=x"):
            with self.subTest(url=url), self.assertRaises(exporter.ExportError):
                exporter.PrometheusReader(url)
        for max_age in (0, -1, float("nan")):
            with self.subTest(max_age=max_age), self.assertRaises(exporter.ExportError):
                exporter.build_rows(sample_series(), WINDOW, max_age)


if __name__ == "__main__":
    unittest.main()
