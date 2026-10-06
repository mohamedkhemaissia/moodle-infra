"""Tests de fusion, d'integrite et de traitement des interruptions."""

from contextlib import redirect_stderr, redirect_stdout
import csv
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from ml import export_metrics as exporter
from ml import prepare_dataset as preparation


BASE = 1_700_000_010
WINDOW = exporter.Window(BASE, BASE + 60, 30)


class FixtureReader:
    def __init__(self, window, overrides=None):
        self.url = "http://127.0.0.1:9090"
        defaults = {
            "cpu_usage_percent": 0.0, "memory_usage_percent": 25.0,
            "disk_available_percent": 40.0, "moodle_available": 1.0,
            "moodle_probe_duration_ms": 75.0, "moodle_http_status_code": 200.0,
            "linux_up": 1.0, "linux_age": 5.0,
            "moodle_https_up": 1.0, "moodle_https_age": 10.0,
        }
        defaults.update(overrides or {})
        self.series = {}
        for name, value in defaults.items():
            values = value if isinstance(value, dict) else {index: value for index in range(window.count)}
            self.series[name] = exporter.Series(values, {})

    def query(self, expression, window):
        name = next(name for name, query in exporter.QUERIES.items() if query == expression)
        return self.series[name]


def source_export(root, name, *, window=WINDOW, created_offset=0, overrides=None, max_age=60):
    with patch.object(exporter.time, "time", return_value=BASE + 3600 + created_offset):
        _, directory, _ = exporter.perform_export(
            FixtureReader(window, overrides), window, root / name, max_age=max_age
        )
    return directory / "metadata.json"


def mutate_csv(metadata_path, mutation):
    path = metadata_path.parent / "metrics.csv"
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    mutation(rows)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=exporter.CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    metadata = json.loads(metadata_path.read_text())
    metadata["csv_sha256"] = exporter.file_sha256(path)
    metadata_path.write_text(json.dumps(metadata))


class MergeTests(unittest.TestCase):
    def test_overlap_is_deduplicated_and_whole_complete_rows_are_preferred(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = source_export(root, "first")
            second = source_export(
                root, "second", window=exporter.Window(BASE + 30, BASE + 90, 30),
                created_offset=60,
                overrides={"cpu_usage_percent": {1: 10.0, 2: 10.0}, "memory_usage_percent": 90.0},
            )
            rows, metadata = preparation.merge_exports([first, second])
            self.assertEqual(len(rows), 4)
            self.assertEqual(metadata["summary"]["candidate_rows"], 6)
            self.assertEqual(metadata["summary"]["duplicate_rows_removed"], 2)
            self.assertEqual(rows[1]["cpu_usage_percent"], 0.0)
            self.assertEqual(rows[1]["memory_usage_percent"], 25.0)
            self.assertEqual(rows[1]["source_created_at"], exporter.utc_iso(BASE + 3600))
            self.assertEqual(rows[2]["memory_usage_percent"], 90.0)
            self.assertEqual(metadata["summary"]["resource_complete_rows"], 4)
            self.assertEqual(metadata["summary"]["conflicting_timestamps"], 2)

    def test_newer_complete_failure_preserves_zero_values_and_is_not_a_gap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = source_export(root, "first")
            second = source_export(root, "second", created_offset=60, overrides={
                "moodle_available": 0.0, "moodle_http_status_code": 0.0,
            })
            rows, metadata = preparation.merge_exports([first, second])
            self.assertTrue(all(row["cpu_usage_percent"] == 0.0 for row in rows))
            self.assertTrue(all(row["moodle_available"] == 0 for row in rows))
            self.assertTrue(all(row["moodle_http_status_code"] == 0 for row in rows))
            self.assertEqual(metadata["summary"]["failed_probe_rows"], 3)
            self.assertEqual(metadata["summary"]["all_metrics_segments"], 1)

    def test_uncovered_times_are_inserted_as_unknown_and_split_segments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = source_export(root, "first", window=exporter.Window(BASE, BASE + 30, 30))
            second = source_export(root, "second", window=exporter.Window(BASE + 120, BASE + 150, 30))
            rows, metadata = preparation.merge_exports([first, second])
            self.assertEqual(len(rows), 6)
            self.assertEqual(metadata["summary"]["uncovered_grid_rows"], 2)
            self.assertEqual([row["resource_segment_id"] for row in rows], [1, 1, None, None, 2, 2])
            self.assertIsNone(rows[2]["cpu_usage_percent"])
            self.assertIsNone(rows[2]["moodle_available"])
            self.assertIsNone(rows[2]["source_export_id"])
            self.assertEqual(rows[2]["linux_collection_status"], "missing")

    def test_an_incomplete_resource_row_splits_only_the_resource_segment(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = source_export(Path(temporary), "first", overrides={"cpu_usage_percent": {0: 0.0, 2: 0.0}})
            rows, metadata = preparation.merge_exports([path])
            self.assertEqual([row["resource_segment_id"] for row in rows], [1, None, 2])
            self.assertEqual(metadata["summary"]["resource_segments"], 2)
            self.assertIsNone(rows[1]["cpu_usage_percent"])
            self.assertEqual(rows[1]["moodle_available"], 1)

    def test_conflicts_between_discarded_partial_rows_are_still_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            only_cpu = {name: None for name in exporter.METRIC_QUERIES}
            only_cpu["cpu_usage_percent"] = 1.0
            first = source_export(root, "first", overrides=only_cpu)
            only_moodle = {name: None for name in exporter.RESOURCE_METRICS}
            second = source_export(root, "second", created_offset=60, overrides=only_moodle)
            only_cpu["cpu_usage_percent"] = 2.0
            third = source_export(root, "third", created_offset=120, overrides=only_cpu)
            rows, metadata = preparation.merge_exports([first, second, third])
            self.assertIsNone(rows[0]["cpu_usage_percent"])
            self.assertEqual(metadata["summary"]["conflicting_timestamps"], 3)

    def test_identical_copies_are_verified_then_skipped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = source_export(root, "first")
            copied = root / "copied" / first.parent.name
            shutil.copytree(first.parent, copied)
            rows, metadata = preparation.merge_exports([first, copied / "metadata.json"])
            self.assertEqual(len(rows), 3)
            self.assertEqual(metadata["summary"]["sources_verified"], 1)
            self.assertEqual(metadata["summary"]["identical_sources_skipped"], 1)
            self.assertEqual(metadata["summary"]["candidate_rows"], 3)

    def test_different_collection_policies_and_different_steps_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = source_export(root, "first")
            policy = source_export(root, "policy", max_age=90)
            step = source_export(root, "step", window=exporter.Window(BASE + 30, BASE + 90, 60))
            for other in (policy, step):
                with self.subTest(other=other), self.assertRaisesRegex(preparation.ExportError, "meme configuration"):
                    preparation.merge_exports([first, other])


class IntegrityTests(unittest.TestCase):
    def test_csv_checksum_corruption_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = source_export(Path(temporary), "first")
            with (path.parent / "metrics.csv").open("a") as stream:
                stream.write("corrompu\n")
            with self.assertRaisesRegex(preparation.ExportError, "empreinte"):
                preparation.read_export(path)

    def test_invalid_values_flags_and_timestamps_are_rejected_even_with_a_valid_checksum(self):
        mutations = (
            lambda rows: rows[0].update(cpu_usage_percent="NaN"),
            lambda rows: rows[0].update(memory_usage_percent="101"),
            lambda rows: rows[0].update(moodle_http_status_code="200.5"),
            lambda rows: rows[0].update(resource_metrics_complete="0"),
            lambda rows: rows.reverse(),
            lambda rows: rows.pop(),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                path = source_export(Path(temporary), "first")
                mutate_csv(path, mutation)
                with self.assertRaises(preparation.ExportError):
                    preparation.read_export(path)

    def test_inconsistent_summary_and_malformed_metadata_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = source_export(Path(temporary), "first")
            original = json.loads(path.read_text())
            wrong_summary = json.loads(path.read_text())
            wrong_summary["summary"]["resource_complete_rows"] = 99
            malformed_url = {**original, "prometheus_url": []}
            for metadata in (wrong_summary, malformed_url, []):
                path.write_text(json.dumps(metadata))
                with self.subTest(metadata=metadata), self.assertRaises(preparation.ExportError):
                    preparation.read_export(path)

    def test_rounded_age_on_the_freshness_boundary_remains_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = source_export(Path(temporary), "first", overrides={"linux_age": {0: 60.0004, 1: 5.0, 2: 5.0}})
            source = preparation.read_export(path)
            self.assertEqual(source.rows[0]["linux_collection_status"], "stale")
            self.assertEqual(source.rows[0]["linux_scrape_age_seconds"], 60.0)
            self.assertIsNone(source.rows[0]["cpu_usage_percent"])

    def test_unpublished_directories_are_not_discovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hidden = root / ".export-incomplete"
            hidden.mkdir()
            (hidden / "metadata.json").write_text("{}")
            with self.assertRaisesRegex(preparation.ExportError, "Aucun export publie"):
                preparation.discover_exports([root])


class PublicationTests(unittest.TestCase):
    def test_csv_metadata_and_repeated_publication_are_consistent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = source_export(root, "input", overrides={"cpu_usage_percent": {0: 0.0, 2: 0.0}})
            rows, metadata = preparation.merge_exports([source])
            published, directory, reused = preparation.publish_dataset(rows, metadata, root / "output")
            self.assertFalse(reused)
            with (directory / "dataset.csv").open(newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, list(preparation.FIELDS))
                stored = list(reader)
            self.assertEqual(stored[0]["cpu_usage_percent"], "0.0")
            self.assertEqual(stored[1]["cpu_usage_percent"], "")
            self.assertEqual(stored[1]["resource_segment_id"], "")
            self.assertEqual(published["dataset_sha256"], exporter.file_sha256(directory / "dataset.csv"))
            again, same, reused = preparation.publish_dataset(rows, metadata, root / "output")
            self.assertTrue(reused)
            self.assertEqual(again, published)
            self.assertEqual(directory, same)
            self.assertEqual(list((root / "output").iterdir()), [directory])

    def test_input_order_does_not_change_the_merged_dataset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = source_export(root, "first")
            second = source_export(root, "second", created_offset=60, overrides={"cpu_usage_percent": 10.0})
            rows, metadata = preparation.merge_exports([first, second])
            reverse_rows, reverse_metadata = preparation.merge_exports([second, first])
            self.assertEqual(rows, reverse_rows)
            self.assertEqual(metadata["input_sha256"], reverse_metadata["input_sha256"])
            self.assertEqual(metadata["summary"], reverse_metadata["summary"])

    def test_corrupted_existing_dataset_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = source_export(root, "input")
            rows, metadata = preparation.merge_exports([source])
            _, directory, _ = preparation.publish_dataset(rows, metadata, root / "output")
            (directory / "dataset.csv").write_text("corrompu")
            with self.assertRaises(preparation.ExportError):
                preparation.publish_dataset(rows, metadata, root / "output")
            self.assertEqual((directory / "dataset.csv").read_text(), "corrompu")

    def test_cli_reports_a_successful_dataset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_export(root, "input")
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                code = preparation.main(["--input-dir", str(root / "input"), "--output-dir", str(root / "output")])
            report = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(report["status"], "prepared")
            self.assertEqual(report["resource_complete_rows"], 3)
            self.assertTrue(Path(report["dataset_file"]).exists())

    def test_cli_failure_does_not_publish_partial_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = source_export(root, "input")
            (path.parent / "metrics.csv").write_text("corrompu")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = preparation.main(["--input-dir", str(root / "input"), "--output-dir", str(root / "output")])
            self.assertEqual(code, 1)
            self.assertFalse((root / "output").exists())

    def test_cli_preserves_an_empty_export_and_reports_no_usable_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_export(root, "input", overrides={name: None for name in exporter.QUERIES})
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                code = preparation.main(["--input-dir", str(root / "input"), "--output-dir", str(root / "output")])
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(stdout.getvalue())["status"], "no_usable_resource_data")


if __name__ == "__main__":
    unittest.main()
