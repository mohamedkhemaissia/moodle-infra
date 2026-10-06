"""Verifier les calculs, la chronologie, les interruptions et les artefacts."""

from contextlib import redirect_stderr, redirect_stdout
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ml import export_metrics as exporter
from ml import prepare_dataset as preparation
from ml import prepare_features as features


BASE = 1_700_000_010


class FixtureReader:
    def __init__(self, window, overrides):
        self.url = "http://127.0.0.1:9090"
        values = {
            "cpu_usage_percent": {index: round(index * 0.02, 3) for index in range(window.count)},
            "memory_usage_percent": 25.0, "disk_available_percent": 40.0,
            "moodle_available": 1.0, "moodle_probe_duration_ms": 75.0,
            "moodle_http_status_code": 200.0,
            "linux_up": 1.0, "linux_age": 5.0,
            "moodle_https_up": 1.0, "moodle_https_age": 10.0,
        }
        values.update(overrides or {})
        self.series = {name: exporter.Series(
            value if isinstance(value, dict) else {index: value for index in range(window.count)}, {}
        ) for name, value in values.items()}

    def query(self, expression, window):
        name = next(name for name, query in exporter.QUERIES.items() if query == expression)
        return self.series[name]


def make_dataset(root, *, count=401, overrides=None, step=30):
    start = BASE // step * step
    window = exporter.Window(start, start + (count - 1) * step, step)
    _, exported, _ = exporter.perform_export(FixtureReader(window, overrides), window, root / "exports")
    rows, metadata = preparation.merge_exports([exported / "metadata.json"])
    _, directory, _ = preparation.publish_dataset(rows, metadata, root / "datasets")
    return directory


def change_csv(directory, change):
    path = directory / "dataset.csv"
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        fields, rows = reader.fieldnames, list(reader)
    change(rows)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    metadata_path = directory / "metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["dataset_sha256"] = exporter.file_sha256(path)
    metadata_path.write_text(json.dumps(metadata))


class CalculationTests(unittest.TestCase):
    def test_statistics_use_elapsed_minutes_and_population_standard_deviation(self):
        stats = features.window_statistics([0.0, 10.0, 20.0], 30)
        self.assertEqual(stats["current"], 20)
        self.assertEqual(stats["mean"], 10)
        self.assertEqual(stats["min"], 0)
        self.assertEqual(stats["max"], 20)
        self.assertEqual(stats["delta"], 20)
        self.assertEqual(stats["slope_per_minute"], 20)
        self.assertAlmostEqual(stats["std"], (200 / 3) ** 0.5, places=6)
        self.assertEqual(features.window_statistics([0.0] * 11, 30)["std"], 0)
        self.assertEqual(features.window_statistics([20.0, 10.0, 0.0], 30)["slope_per_minute"], -20)

    def test_future_change_does_not_change_past_features_but_changes_exact_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = features.read_dataset(make_dataset(root / "first"))
            changed_cpu = {index: round(index * 0.02, 3) for index in range(401)}
            changed_cpu[200] = 95.0
            second = features.read_dataset(make_dataset(root / "second", overrides={"cpu_usage_percent": changed_cpu}))
            first_tables, _ = features.build_tables(first)
            second_tables, _ = features.build_tables(second)
            changed_at = exporter.utc_iso(BASE + 200 * 30)
            before = lambda rows: [row for row in rows if row["evaluated_at"] < changed_at]
            self.assertEqual(before(first_tables["features.csv"]), before(second_tables["features.csv"]))
            origin = exporter.utc_iso(BASE + 190 * 30)
            original = next(row for row in first_tables["forecast_5m.csv"] if row["evaluated_at"] == origin)
            modified = next(row for row in second_tables["forecast_5m.csv"] if row["evaluated_at"] == origin)
            self.assertEqual(original["target_evaluated_at"], changed_at)
            self.assertEqual(original["target__cpu_usage_percent"], 4)
            self.assertEqual(modified["target__cpu_usage_percent"], 95)
            self.assertTrue(all(original[name] == modified[name] for name in features.FEATURE_NAMES))

    def test_all_horizons_are_exact_and_persistence_baseline_is_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            dataset = features.read_dataset(make_dataset(Path(temporary)))
            tables, metadata = features.build_tables(dataset)
            self.assertEqual(metadata["summary"]["feature_count"], 21)
            self.assertEqual(metadata["window_points_including_endpoints"], 11)
            for minutes in features.HORIZONS_MINUTES:
                rows = tables[f"forecast_{minutes}m.csv"]
                self.assertTrue(rows)
                for row in rows:
                    timestamp = preparation.parse_utc(row["evaluated_at"])
                    self.assertEqual(preparation.parse_utc(row["target_evaluated_at"]) - timestamp, minutes * 60)
                    self.assertAlmostEqual(row["target__cpu_usage_percent"] - row["cpu_usage_percent__current"], minutes * 0.04)
                    self.assertEqual(row["target__memory_usage_percent"], row["memory_usage_percent__current"])

    def test_zero_resources_and_a_failed_known_probe_remain_valid_resource_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = make_dataset(Path(temporary), overrides={
                "cpu_usage_percent": 0.0, "memory_usage_percent": 0.0,
                "disk_available_percent": 0.0, "moodle_available": 0.0, "moodle_http_status_code": 0.0,
            })
            dataset = features.read_dataset(directory)
            tables, _ = features.build_tables(dataset)
            self.assertTrue(tables["features.csv"])
            self.assertTrue(all(row[name] == 0 for row in tables["features.csv"] for name in features.FEATURE_NAMES))
            self.assertEqual(dataset.metadata["summary"]["failed_probe_rows"], 401)


class ChronologyTests(unittest.TestCase):
    def test_partitions_have_disjoint_input_contexts_and_future_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            dataset = features.read_dataset(make_dataset(Path(temporary)))
            tables, metadata = features.build_tables(dataset)
            periods = {item["split"]: item for item in metadata["split_periods"]}
            self.assertGreater(metadata["summary"]["skipped_feature_rows"]["split_boundary"], 0)
            for filename, rows in tables.items():
                for row in rows:
                    period = periods[row["split"]]
                    start = preparation.parse_utc(row["input_context_start"])
                    end = preparation.parse_utc(row.get("target_evaluated_at", row["evaluated_at"]))
                    self.assertGreaterEqual(start, preparation.parse_utc(period["start"]))
                    self.assertLess(end, preparation.parse_utc(period["end_exclusive"]))
                    self.assertEqual(preparation.parse_utc(row["window_start"]) - start, 120)
                for earlier, later in (("train", "validation"), ("validation", "test")):
                    earlier_rows = [row for row in rows if row["split"] == earlier]
                    later_rows = [row for row in rows if row["split"] == later]
                    self.assertTrue(earlier_rows and later_rows, filename)
                    last_target = max(preparation.parse_utc(row.get("target_evaluated_at", row["evaluated_at"]))
                                      for row in earlier_rows)
                    first_context = min(preparation.parse_utc(row["input_context_start"]) for row in later_rows)
                    self.assertLess(last_target, first_context)

    def test_resource_hole_breaks_history_context_and_all_forecast_horizons(self):
        with tempfile.TemporaryDirectory() as temporary:
            cpu = {index: 25.0 for index in range(401) if index != 100}
            dataset = features.read_dataset(make_dataset(Path(temporary), overrides={"cpu_usage_percent": cpu}))
            tables, metadata = features.build_tables(dataset)
            hole = BASE + 100 * 30
            self.assertGreater(metadata["summary"]["skipped_forecast_rows"]["15"]["missing_future_or_gap"], 0)
            for rows in tables.values():
                for row in rows:
                    start = preparation.parse_utc(row["input_context_start"])
                    end = preparation.parse_utc(row.get("target_evaluated_at", row["evaluated_at"]))
                    self.assertFalse(start <= hole <= end)
            after = next(row for row in tables["features.csv"] if preparation.parse_utc(row["evaluated_at"]) > hole)
            self.assertGreaterEqual(preparation.parse_utc(after["evaluated_at"]) - hole, 450)

    def test_missing_moodle_probe_does_not_discard_complete_resource_windows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = features.read_dataset(make_dataset(root / "first"))
            probe = {index: 1.0 for index in range(401) if index != 100}
            second = features.read_dataset(make_dataset(root / "second", overrides={"moodle_available": probe}))
            first_tables, _ = features.build_tables(first)
            second_tables, _ = features.build_tables(second)
            self.assertEqual(first_tables, second_tables)
            self.assertEqual(second.metadata["summary"]["all_metrics_segments"], 2)

    def test_short_partitions_are_reported_without_inventing_forecast_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            dataset = features.read_dataset(make_dataset(Path(temporary), count=50))
            tables, metadata = features.build_tables(dataset)
            self.assertTrue(tables["features.csv"])
            self.assertFalse(metadata["summary"]["splits_available_for_each_horizon"]["15"])
            self.assertEqual(metadata["summary"]["forecast_rows"]["15"]["validation"], 0)
            self.assertEqual(metadata["summary"]["forecast_rows"]["15"]["test"], 0)

    def test_steps_and_lookback_have_explicit_compatible_units(self):
        self.assertEqual(features.validate_settings(30, 5), 11)
        self.assertEqual(features.validate_settings(60, 5), 6)
        for step, minutes in ((45, 5), (600, 5), (30, 0), (30, 61), (30, True)):
            with self.subTest(step=step, minutes=minutes), self.assertRaises(features.FeatureError):
                features.validate_settings(step, minutes)


class IntegrityTests(unittest.TestCase):
    def test_checksum_corruption_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = make_dataset(Path(temporary))
            with (directory / "dataset.csv").open("a") as stream:
                stream.write("corrompu\n")
            with self.assertRaisesRegex(features.FeatureError, "empreinte"):
                features.read_dataset(directory)

    def test_invalid_metrics_flags_segments_and_chronology_are_rejected_even_with_valid_checksum(self):
        mutations = (
            lambda rows: rows[20].update(cpu_usage_percent="NaN"),
            lambda rows: rows[20].update(memory_usage_percent="101"),
            lambda rows: rows[20].update(resource_metrics_complete="0"),
            lambda rows: rows[20].update(resource_segment_id="2"),
            lambda rows: rows[20].update(source_export_id="inconnue"),
            lambda rows: rows[20].update(linux_collection_status="down"),
            lambda rows: rows[20].update(evaluated_at=rows[19]["evaluated_at"]),
            lambda rows: rows.reverse(),
            lambda rows: rows.pop(),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                directory = make_dataset(Path(temporary), count=51)
                change_csv(directory, mutation)
                with self.assertRaises(features.FeatureError):
                    features.read_dataset(directory)

    def test_summary_manifest_and_non_standard_json_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = make_dataset(Path(temporary))
            path = directory / "metadata.json"
            original = path.read_text()
            wrong_summary = json.loads(original)
            wrong_summary["summary"]["resource_complete_rows"] = 1
            wrong_manifest = json.loads(original)
            wrong_manifest["source_metadata_sha256"] = []
            bad_texts = (json.dumps(wrong_summary), json.dumps(wrong_manifest),
                         '{"schema_version":1,"schema_version":1}', '{"value":NaN}', '[]')
            for text in bad_texts:
                path.write_text(text)
                with self.subTest(text=text), self.assertRaises(features.FeatureError):
                    features.read_dataset(directory)

    def test_excessive_input_is_rejected_before_csv_loading(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = make_dataset(Path(temporary), count=51)
            with patch.object(features, "MAX_INPUT_ROWS", 50), self.assertRaisesRegex(features.FeatureError, "trop grand"):
                features.read_dataset(directory)


class PublicationTests(unittest.TestCase):
    def test_bundle_contains_typed_schemas_checksums_and_can_be_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = features.read_dataset(make_dataset(root))
            tables, metadata = features.build_tables(dataset)
            published, directory, reused = features.publish_features(tables, metadata, root / "features")
            self.assertFalse(reused)
            self.assertEqual({path.name for path in directory.iterdir()}, {*tables, "metadata.json"})
            for filename, records in tables.items():
                self.assertEqual(published["files"][filename]["sha256"], exporter.file_sha256(directory / filename))
                with (directory / filename).open(newline="") as stream:
                    reader = csv.DictReader(stream)
                    self.assertEqual(reader.fieldnames, list(features.FEATURE_FIELDS if filename == "features.csv" else features.FORECAST_FIELDS))
                    rows = list(reader)
                self.assertEqual(len(rows), len(records))
                self.assertEqual({row["split"] for row in rows}, set(features.SPLITS))
            again, same, reused = features.publish_features(tables, metadata, root / "features")
            self.assertTrue(reused)
            self.assertEqual(again, published)
            self.assertEqual(same, directory)

    def test_changed_lookback_gets_a_different_immutable_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = features.read_dataset(make_dataset(root))
            first_tables, first_metadata = features.build_tables(dataset, 5)
            second_tables, second_metadata = features.build_tables(dataset, 10)
            _, first, _ = features.publish_features(first_tables, first_metadata, root / "features")
            _, second, _ = features.publish_features(second_tables, second_metadata, root / "features")
            self.assertNotEqual(first, second)
            self.assertNotEqual(first_metadata["input_sha256"], second_metadata["input_sha256"])

    def test_corrupted_existing_output_is_rejected_and_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = features.read_dataset(make_dataset(root))
            tables, metadata = features.build_tables(dataset)
            _, directory, _ = features.publish_features(tables, metadata, root / "features")
            path = directory / "forecast_15m.csv"
            with path.open("a") as stream:
                stream.write("corrompu\n")
            previous = path.read_bytes()
            with self.assertRaisesRegex(features.FeatureError, "corrompu"):
                features.publish_features(tables, metadata, root / "features")
            self.assertEqual(path.read_bytes(), previous)

    def test_failed_atomic_publication_leaves_no_partial_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = features.read_dataset(make_dataset(root))
            tables, metadata = features.build_tables(dataset)
            with patch.object(features.os, "rename", side_effect=OSError("disque indisponible")):
                with self.assertRaises(OSError):
                    features.publish_features(tables, metadata, root / "features")
            self.assertEqual(list((root / "features").iterdir()), [])

    def test_cli_prepares_and_reuses_without_model_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = make_dataset(root)
            argv = ["--dataset-dir", str(directory), "--output-dir", str(root / "features")]
            for status in ("prepared", "already_prepared"):
                output = io.StringIO()
                with redirect_stdout(output):
                    result = features.main(argv)
                self.assertEqual(result, 0)
                self.assertEqual(json.loads(output.getvalue())["status"], status)

    def test_all_missing_input_retains_an_empty_quality_report_and_exit_three(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = make_dataset(root, count=50, overrides={name: None for name in exporter.RESOURCE_METRICS})
            output = io.StringIO()
            with redirect_stdout(output):
                result = features.main(["--dataset-dir", str(directory), "--output-dir", str(root / "features")])
            report = json.loads(output.getvalue())
            self.assertEqual(result, 3)
            self.assertEqual(report["status"], "no_usable_feature_windows")
            self.assertEqual(report["feature_rows"], 0)
            self.assertTrue(Path(report["metadata_file"]).is_file())

    def test_invalid_input_stops_before_any_output_is_published(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            error = io.StringIO()
            with redirect_stderr(error):
                result = features.main(["--dataset-dir", str(root / "absent"), "--output-dir", str(root / "features")])
            self.assertEqual(result, 1)
            self.assertFalse((root / "features").exists())
            self.assertIn("interrompue", error.getvalue())


if __name__ == "__main__":
    unittest.main()
