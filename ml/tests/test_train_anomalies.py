"""Integrite, absence de fuite temporelle et concordance des arbres JSON."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from sklearn.ensemble import IsolationForest
from threadpoolctl import threadpool_limits

from ml import export_metrics as exporter
from ml import prepare_dataset as preparation
from ml import prepare_features as features
from ml import isolation_model as numeric
from ml import train_anomalies as training


BASE = 1_700_000_010


class FixtureReader:
    def __init__(self, count=401, overrides=None):
        self.url = "http://127.0.0.1:9090"
        values = {
            "cpu_usage_percent": {i: 10 + i * .02 for i in range(count)},
            "memory_usage_percent": 25.0, "disk_available_percent": 40.0,
            "moodle_available": 1.0, "moodle_probe_duration_ms": 75.0,
            "moodle_http_status_code": 200.0, "linux_up": 1.0, "linux_age": 5.0,
            "moodle_https_up": 1.0, "moodle_https_age": 10.0,
        }
        values.update(overrides or {})
        self.series = {name: exporter.Series(value if isinstance(value, dict) else {i: value for i in range(count)}, {})
                       for name, value in values.items()}

    def query(self, expression, window):
        name = next(name for name, query in exporter.QUERIES.items() if query == expression)
        return self.series[name]


def make_features(root, count=401, overrides=None):
    window = exporter.Window(BASE, BASE + (count - 1) * 30, 30)
    _, exported, _ = exporter.perform_export(FixtureReader(count, overrides), window, root / "exports")
    rows, metadata = preparation.merge_exports([exported / "metadata.json"])
    _, dataset, _ = preparation.publish_dataset(rows, metadata, root / "datasets")
    tables, metadata = features.build_tables(features.read_dataset(dataset))
    _, directory, _ = features.publish_features(tables, metadata, root / "features")
    return directory


def change_table(directory, change, update_summary=False):
    path = directory / "features.csv"
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
    metadata["files"]["features.csv"].update(sha256=exporter.file_sha256(path), rows=len(rows))
    if update_summary:
        metadata["summary"]["feature_rows"] = len(rows)
        metadata["summary"]["rows_by_split"] = {name: sum(r["split"] == name for r in rows) for name in features.SPLITS}
    metadata_path.write_text(json.dumps(metadata))


class InputTests(unittest.TestCase):
    def test_valid_inputs_use_only_feature_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = make_features(Path(temporary))
            inputs = training.read_inputs(directory)
            self.assertGreater(len(inputs.rows), 300)
            self.assertEqual(list(inputs.rows[0])[3:], list(features.FEATURE_NAMES))

    def test_modified_csv_fails_its_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = make_features(Path(temporary))
            with (directory / "features.csv").open("a") as stream:
                stream.write("\n")
            with self.assertRaises(training.AnomalyError):
                training.read_inputs(directory)

    def test_non_finite_values_are_rejected_even_with_updated_checksums(self):
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary:
                directory = make_features(Path(temporary))
                change_table(directory, lambda rows: rows[0].update(cpu_usage_percent__current=value))
                with self.assertRaises(training.AnomalyError):
                    training.read_inputs(directory)

    def test_duplicates_and_false_contiguous_segments_are_rejected(self):
        for change in (lambda rows: rows.__setitem__(1, copy.deepcopy(rows[0])),
                       lambda rows: rows.pop(10)):
            with tempfile.TemporaryDirectory() as temporary:
                directory = make_features(Path(temporary))
                change_table(directory, change, update_summary=True)
                with self.assertRaises(training.AnomalyError):
                    training.read_inputs(directory)

    def test_past_context_cannot_cross_a_partition_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = make_features(Path(temporary))
            def change(rows):
                next(row for row in rows if row["split"] == "train")["split"] = "validation"
            change_table(directory, change, update_summary=True)
            with self.assertRaises(training.AnomalyError):
                training.read_inputs(directory)

    def test_malformed_metadata_and_inconsistent_statistics_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = make_features(Path(temporary))
            change_table(directory, lambda rows: rows[0].update(memory_usage_percent__mean="101"))
            with self.assertRaises(training.AnomalyError):
                training.read_inputs(directory)
        with tempfile.TemporaryDirectory() as temporary:
            directory = make_features(Path(temporary))
            path = directory / "metadata.json"
            path.write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaises(training.AnomalyError):
                training.read_inputs(directory)

    def test_short_partitions_do_not_train(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(training.AnomalyError):
                training.read_inputs(make_features(Path(temporary), count=61))

    def test_valid_collection_gaps_are_separate_segments(self):
        with tempfile.TemporaryDirectory() as temporary:
            ages = {i: 90.0 if 80 <= i < 88 else 5.0 for i in range(401)}
            inputs = training.read_inputs(make_features(Path(temporary), overrides={"linux_age": ages}))
            self.assertGreater(len({r["resource_segment_id"] for r in inputs.rows}), 1)
            self.assertFalse(any(BASE + 80 * 30 <= preparation.parse_utc(r["evaluated_at"]) < BASE + 88 * 30
                                 for r in inputs.rows))


class NumericModelTests(unittest.TestCase):
    def fit_model(self):
        rng = np.random.default_rng(42)
        values = rng.normal(size=(128, 3))
        with threadpool_limits(limits=1):
            forest = IsolationForest(n_estimators=20, max_samples=128, random_state=42, n_jobs=1).fit(values)
        return forest, values, numeric.export_forest(forest, ["cpu", "ram", "disk"], .6)

    def test_json_replay_matches_sklearn_on_new_values_and_float32_boundaries(self):
        forest, values, model = self.fit_model()
        tree = forest.estimators_[0].tree_
        feature = tree.feature[0]
        threshold = np.float32(tree.threshold[0])
        borders = np.tile(values[0], (3, 1))
        borders[:, feature] = [np.nextafter(threshold, np.float32(-np.inf)), threshold,
                               np.nextafter(threshold, np.float32(np.inf))]
        evaluation = np.vstack((values, [[-20, 50, -8], [80, 100, 0]], borders))
        restored = json.loads(json.dumps(model, allow_nan=False))
        np.testing.assert_allclose(numeric.score_rows(restored, evaluation.tolist()),
                                   -forest.score_samples(evaluation), rtol=0, atol=1e-10)

    def test_cycles_and_unreachable_nodes_are_rejected(self):
        _, _, original = self.fit_model()
        changed = copy.deepcopy(original)
        changed["trees"][0]["left"][0] = 0
        with self.assertRaises(numeric.ModelError):
            numeric.validate_model(changed)
        changed = copy.deepcopy(original)
        for key in ("left", "right", "feature", "threshold", "samples"):
            changed["trees"][0][key].append(changed["trees"][0][key][-1])
        with self.assertRaises(numeric.ModelError):
            numeric.validate_model(changed)

    def test_invalid_values_features_and_thresholds_are_rejected(self):
        _, _, model = self.fit_model()
        for row in ([1, 2], [True, 2, 3], [float("nan"), 2, 3], [1e100, 2, 3]):
            with self.subTest(row=row), self.assertRaises(numeric.ModelError):
                numeric.score_rows(model, [row])
        with self.assertRaises(numeric.ModelError):
            numeric.validate_model(model, ["ram", "cpu", "disk"])
        for threshold in (True, 0, 1, float("nan")):
            bad = {**model, "anomaly_threshold": threshold}
            with self.assertRaises(numeric.ModelError):
                numeric.validate_model(bad)

    def test_load_rejects_duplicate_keys_and_nonstandard_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "model.json"
            for data in ('{"x":1,"x":2}', '{"x":NaN}', "[]"):
                path.write_text(data)
                with self.assertRaises(numeric.ModelError):
                    numeric.load_model(path)


class TrainingTests(unittest.TestCase):
    def test_validation_and_test_inputs_never_change_fit_or_threshold(self):
        with tempfile.TemporaryDirectory() as temporary:
            original = training.read_inputs(make_features(Path(temporary)))
            changed = copy.deepcopy(original)
            for row in changed.rows:
                if row["split"] != "train":
                    for metric in exporter.RESOURCE_METRICS:
                        for stat in ("current", "mean", "min", "max"):
                            row[f"{metric}__{stat}"] = 99.0
            first_model, first_report, first_scores = training.fit_and_score(original)
            second_model, second_report, second_scores = training.fit_and_score(changed)
            self.assertEqual(first_model, second_model)
            self.assertEqual(first_report["by_split"]["train"], second_report["by_split"]["train"])
            self.assertEqual(first_report["anomaly_threshold"], second_report["anomaly_threshold"])
            self.assertNotEqual(first_scores, second_scores)

    def test_constant_data_and_zero_cpu_do_not_force_anomalies(self):
        with tempfile.TemporaryDirectory() as temporary:
            inputs = training.read_inputs(make_features(Path(temporary), overrides={"cpu_usage_percent": 0.0}))
            _, details, scores = training.fit_and_score(inputs)
            self.assertAlmostEqual(details["anomaly_threshold"], .5, places=12)
            self.assertEqual(len(details["constant_training_features"]), 21)
            self.assertTrue(all(not row["statistical_anomaly"] for row in scores))

    def test_statistics_do_not_claim_accuracy_without_incident_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            inputs = training.read_inputs(make_features(Path(temporary)))
            _, details, scores = training.fit_and_score(inputs)
            self.assertEqual(details["detection_quality"]["status"], "not_evaluated_no_incident_labels")
            self.assertIsNone(details["detection_quality"]["precision"])
            self.assertIsNone(details["detection_quality"]["recall"])
            self.assertIsNone(details["detection_quality"]["false_positive_rate"])
            self.assertEqual(sum(v["rows"] for v in details["by_split"].values()), len(scores))
            self.assertLessEqual(details["json_replay_max_absolute_difference"], 1e-10)
            self.assertLessEqual(details["by_split"]["train"]["statistical_anomaly_rate_percent"], 2.000001)

    def test_operational_rules_are_separate_from_statistical_scores(self):
        normal = {"cpu_usage_percent__current": 10, "memory_usage_percent__current": 30,
                  "disk_available_percent__current": 40}
        self.assertEqual(training.operational_reasons(normal), [])
        raised = {"cpu_usage_percent__current": 85, "memory_usage_percent__current": 90,
                  "disk_available_percent__current": 10}
        self.assertEqual(training.operational_reasons(raised), list(exporter.RESOURCE_METRICS))

    def test_files_are_atomic_idempotent_and_corruption_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = training.read_inputs(make_features(root))
            report, directory, reused = training.perform_training(inputs, root / "models")
            self.assertFalse(reused)
            for name in ("model.json", "scores.csv"):
                self.assertEqual(report["files"][name]["sha256"], exporter.file_sha256(directory / name))
            restored = numeric.load_model(directory / "model.json", list(features.FEATURE_NAMES))
            self.assertEqual(len(restored["trees"]), 100)
            same, same_path, reused = training.perform_training(inputs, root / "models")
            self.assertTrue(reused)
            self.assertEqual(same, report)
            self.assertEqual(same_path, directory)
            path = directory / "scores.csv"
            path.write_text("corrompu\n")
            with self.assertRaises(training.AnomalyError):
                training.perform_training(inputs, root / "models")
            self.assertEqual(path.read_text(), "corrompu\n")

    def test_cli_produces_json_and_invalid_input_returns_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = make_features(root)
            output, error = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                code = training.main(["--features-dir", str(directory), "--output-dir", str(root / "models")])
            result = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "trained_experimental")
            self.assertTrue(Path(result["model_file"]).is_file())
            self.assertNotIn("accuracy", result)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(training.main(["--features-dir", str(root / "absent")]), 1)


if __name__ == "__main__":
    unittest.main()
