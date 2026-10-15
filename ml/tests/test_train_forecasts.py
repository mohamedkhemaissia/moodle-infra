"""Tests d'integrite, de fuite temporelle et d'evaluation des regressions."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from ml import export_metrics as exporter
from ml import prepare_dataset as preparation
from ml import prepare_features as features
from ml import train_forecasts as training


BASE = 1_700_000_010


class FixtureReader:
    def __init__(self, count, overrides=None):
        self.url = "http://127.0.0.1:9090"
        values = {
            "cpu_usage_percent": {index: round(index * 0.02, 3) for index in range(count)},
            "memory_usage_percent": 25.0, "disk_available_percent": 40.0,
            "moodle_available": 1.0, "moodle_probe_duration_ms": 75.0,
            "moodle_http_status_code": 200.0,
            "linux_up": 1.0, "linux_age": 5.0, "moodle_https_up": 1.0, "moodle_https_age": 10.0,
        }
        values.update(overrides or {})
        self.series = {name: exporter.Series(value if isinstance(value, dict) else {index: value for index in range(count)}, {})
                       for name, value in values.items()}

    def query(self, expression, window):
        name = next(name for name, query in exporter.QUERIES.items() if query == expression)
        return self.series[name]


def make_features(root, *, count=401, overrides=None):
    window = exporter.Window(BASE, BASE + (count - 1) * 30, 30)
    _, exported, _ = exporter.perform_export(FixtureReader(count, overrides), window, root / "exports")
    rows, metadata = preparation.merge_exports([exported / "metadata.json"])
    _, dataset, _ = preparation.publish_dataset(rows, metadata, root / "datasets")
    tables, metadata = features.build_tables(features.read_dataset(dataset))
    _, directory, _ = features.publish_features(tables, metadata, root / "features")
    return directory


def change_table(directory, filename, change):
    path = directory / filename
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
    metadata["files"][filename]["sha256"] = exporter.file_sha256(path)
    metadata["files"][filename]["rows"] = len(rows)
    metadata_path.write_text(json.dumps(metadata))


class EvaluationTests(unittest.TestCase):
    def test_mae_rmse_and_bias_have_known_values(self):
        actual = np.asarray([[10, 25, 40], [14, 25, 40]], dtype=float)
        predicted = np.asarray([[12, 26, 39], [12, 24, 41]], dtype=float)
        result = training.error_scores(actual, predicted)
        self.assertEqual(result["cpu_usage_percent"], {"mae": 2, "rmse": 2, "bias": 0})
        self.assertEqual(result["memory_usage_percent"], {"mae": 1, "rmse": 1, "bias": 0})
        self.assertEqual(result["disk_available_percent"], {"mae": 1, "rmse": 1, "bias": 0})
        with self.assertRaises(training.TrainingError):
            training.error_scores(actual, np.full((2, 3), np.nan))

    def test_linear_trend_uses_only_current_values_and_historical_slopes(self):
        rows = [{
            "cpu_usage_percent__current": 50.0, "cpu_usage_percent__slope_per_minute": 2.0,
            "memory_usage_percent__current": 30.0, "memory_usage_percent__slope_per_minute": -1.0,
            "disk_available_percent__current": 99.0, "disk_available_percent__slope_per_minute": 1.0,
            "target__cpu_usage_percent": 1.0, "target__memory_usage_percent": 99.0,
            "target__disk_available_percent": 50.0,
        }]
        np.testing.assert_array_equal(training.linear_trend_predictions(rows, 5, clip=False)[0], [60, 25, 104])
        np.testing.assert_array_equal(training.linear_trend_predictions(rows, 5)[0], [60, 25, 100])
        changed_targets = copy.deepcopy(rows)
        for metric in training.METRICS:
            changed_targets[0][f"target__{metric}"] = 0.0
        np.testing.assert_array_equal(
            training.linear_trend_predictions(rows, 5),
            training.linear_trend_predictions(changed_targets, 5),
        )
        with self.assertRaises(training.TrainingError):
            training.linear_trend_predictions([], 5)

    def test_predictor_selection_uses_validation_and_prefers_simplicity_on_ties(self):
        self.assertEqual(training.preferred_predictor(2.0, 2.0, 2.0), "persistence")
        self.assertEqual(training.preferred_predictor(2.0, 3.0, 1.0), "linear_trend")
        self.assertEqual(training.preferred_predictor(0.5, 3.0, 1.0), "ridge")

    def test_future_test_targets_do_not_change_training_or_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            inputs = training.read_inputs(make_features(Path(temporary)))
            original = inputs.forecasts[5]
            changed = copy.deepcopy(original)
            for row in changed:
                if row["split"] == "test":
                    for metric in training.METRICS:
                        row[f"target__{metric}"] = 95.0
            with threadpool_limits(limits=1):
                first_model, first_report, _ = training.train_horizon(original, 5)
                second_model, second_report, _ = training.train_horizon(changed, 5)
            self.assertEqual(first_model, second_model)
            self.assertEqual(first_report["selected_alpha"], second_report["selected_alpha"])
            self.assertEqual(first_report["candidates"], second_report["candidates"])
            self.assertEqual(first_report["validation"], second_report["validation"])
            self.assertNotEqual(first_report["test"], second_report["test"])

    def test_future_test_inputs_do_not_change_scaler_or_coefficients(self):
        with tempfile.TemporaryDirectory() as temporary:
            inputs = training.read_inputs(make_features(Path(temporary)))
            original = inputs.forecasts[5]
            changed = copy.deepcopy(original)
            for row in changed:
                if row["split"] == "test":
                    for name in features.FEATURE_NAMES:
                        row[name] = 50.0
            with threadpool_limits(limits=1):
                first, _, _ = training.train_horizon(original, 5)
                second, _, _ = training.train_horizon(changed, 5)
            self.assertEqual(first, second)
            train_rows = [row for row in original if row["split"] == "train"]
            x_train, _, _ = training.matrices(train_rows)
            np.testing.assert_allclose(first["scaler_mean"], x_train.mean(axis=0), rtol=0, atol=1e-12)

    def test_reference_can_win_and_zero_reference_error_keeps_gain_null(self):
        with tempfile.TemporaryDirectory() as temporary:
            cpu = {index: 0.0 if index < 280 else 80.0 for index in range(401)}
            inputs = training.read_inputs(make_features(Path(temporary), overrides={"cpu_usage_percent": cpu}))
            with threadpool_limits(limits=1):
                model, report, predictions = training.train_horizon(inputs.forecasts[5], 5)
            self.assertEqual(model["preferred_on_validation"]["cpu_usage_percent"], "persistence")
            comparison = report["test"]["cpu_usage_percent"]
            self.assertEqual(comparison["persistence"]["mae"], 0)
            self.assertEqual(comparison["ridge"]["mae"], 80)
            self.assertIsNone(comparison["mae_gain_percent"])
            self.assertTrue(all(row["preferred_prediction"] == row["persistence"]
                                for row in predictions if row["metric"] == "cpu_usage_percent"))
            json.dumps(report, allow_nan=False)

    def test_json_coefficients_reproduce_the_actual_sklearn_pipeline(self):
        with tempfile.TemporaryDirectory() as temporary:
            inputs = training.read_inputs(make_features(Path(temporary)))
            rows = inputs.forecasts[10]
            with threadpool_limits(limits=1):
                model, _, _ = training.train_horizon(rows, 10)
                x_train, y_train, _ = training.matrices([row for row in rows if row["split"] == "train"])
                x_test, _, _ = training.matrices([row for row in rows if row["split"] == "test"])
                pipeline = make_pipeline(StandardScaler(), Ridge(alpha=model["alpha"], solver="svd"))
                pipeline.fit(x_train, y_train)
                restored = json.loads(json.dumps(model, allow_nan=False))
                np.testing.assert_allclose(training.predict_record(restored, x_test, clip=False),
                                           pipeline.predict(x_test), rtol=0, atol=1e-10)

    def test_clipping_is_explicit_and_invalid_model_arrays_are_rejected(self):
        model = {"scaler_mean": [0.0] * 21, "scaler_scale": [1.0] * 21,
                 "coefficients": [[0.0] * 21 for _ in range(3)], "intercept": [-5.0, 105.0, 40.0]}
        x = np.zeros((2, 21))
        np.testing.assert_array_equal(training.predict_record(model, x)[0], [0, 100, 40])
        np.testing.assert_array_equal(training.predict_record(model, x, clip=False)[0], [-5, 105, 40])
        model["scaler_scale"][0] = 0
        with self.assertRaises(training.TrainingError):
            training.predict_record(model, x)

    def test_invalid_regularization_is_rejected(self):
        for alphas in ((), (0.0,), (-1.0,), (float("nan"),), (True,)):
            with self.subTest(alphas=alphas), self.assertRaises(training.TrainingError):
                training.train_horizon([], 5, alphas=alphas)


class InputTests(unittest.TestCase):
    def test_three_horizons_and_all_partitions_are_loaded_without_changing_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = make_features(Path(temporary))
            before = {path.name: exporter.file_sha256(path) for path in directory.iterdir()}
            inputs = training.read_inputs(directory)
            self.assertEqual(set(inputs.forecasts), {5, 10, 15})
            self.assertTrue(all(set(row["split"] for row in rows) == set(features.SPLITS) for rows in inputs.forecasts.values()))
            self.assertEqual(before, {path.name: exporter.file_sha256(path) for path in directory.iterdir()})

    def test_corrupted_checksum_stops_before_any_fit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = make_features(root)
            with (directory / "forecast_5m.csv").open("a") as stream:
                stream.write("corrompu\n")
            error = io.StringIO()
            with patch.object(training, "train_horizon") as fit, redirect_stderr(error):
                result = training.main(["--features-dir", str(directory), "--output-dir", str(root / "models")])
            self.assertEqual(result, 1)
            fit.assert_not_called()
            self.assertFalse((root / "models").exists())
            self.assertIn("Empreinte", error.getvalue())

    def test_forged_targets_and_cross_partition_rows_are_rejected_with_valid_checksums(self):
        mutations = (
            lambda rows: rows[0].update(target__cpu_usage_percent="NaN"),
            lambda rows: rows[0].update(target__cpu_usage_percent="99"),
            lambda rows: rows[0].update(target_evaluated_at=rows[0]["evaluated_at"]),
            lambda rows: rows[0].update(split="test"),
            lambda rows: rows[0].update(resource_segment_id="999"),
            lambda rows: rows.reverse(),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                directory = make_features(Path(temporary))
                change_table(directory, "forecast_5m.csv", mutation)
                with self.assertRaises(training.TrainingError):
                    training.read_inputs(directory)

    def test_non_finite_and_out_of_domain_features_are_rejected_with_valid_checksums(self):
        mutations = (
            lambda rows: rows[0].update(cpu_usage_percent__current="101"),
            lambda rows: rows[0].update(memory_usage_percent__std="-1"),
            lambda rows: rows[0].update(disk_available_percent__slope_per_minute="inf"),
            lambda rows: rows[0].update(input_context_start=rows[0]["window_start"]),
            lambda rows: rows[0].update(cpu_usage_percent__mean="99"),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                directory = make_features(Path(temporary))
                change_table(directory, "features.csv", mutation)
                with self.assertRaises(training.TrainingError):
                    training.read_inputs(directory)

    def test_malformed_metadata_and_summary_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = make_features(Path(temporary))
            path = directory / "metadata.json"
            original = json.loads(path.read_text())
            wrong_names = copy.deepcopy(original)
            wrong_names["feature_names"].reverse()
            wrong_summary = copy.deepcopy(original)
            wrong_summary["summary"]["feature_rows"] += 1
            overlap = copy.deepcopy(original)
            overlap["split_periods"][1]["start"] = overlap["split_periods"][0]["start"]
            for text in (json.dumps(wrong_names), json.dumps(wrong_summary), json.dumps(overlap), '{"x":NaN}', '[]'):
                path.write_text(text)
                with self.subTest(text=text[:100]), self.assertRaises(training.TrainingError):
                    training.read_inputs(directory)

    def test_insufficient_validation_data_blocks_training(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = make_features(Path(temporary), count=201)
            with self.assertRaisesRegex(training.TrainingError, "au moins"):
                training.read_inputs(directory)


class PublicationTests(unittest.TestCase):
    def test_models_predictions_and_error_report_are_consistent_and_reusable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = training.read_inputs(make_features(root))
            report, directory, reused = training.perform_training(inputs, root / "models")
            self.assertFalse(reused)
            self.assertEqual({path.name for path in directory.iterdir()}, {"models.json", "predictions.csv", "report.json"})
            models = json.loads((directory / "models.json").read_text())
            self.assertEqual(set(models["horizons"]), {"5", "10", "15"})
            with (directory / "predictions.csv").open(newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, list(training.PREDICTION_FIELDS))
                predictions = list(reader)
            self.assertEqual(report["files"]["predictions.csv"]["rows"], len(predictions))
            for filename in ("models.json", "predictions.csv"):
                self.assertEqual(report["files"][filename]["sha256"], exporter.file_sha256(directory / filename))
            for minutes in (5, 10, 15):
                for split in ("validation", "test"):
                    for metric in training.METRICS:
                        rows = [row for row in predictions if int(row["horizon_minutes"]) == minutes
                                and row["split"] == split and row["metric"] == metric]
                        errors = [abs(float(row["ridge"]) - float(row["actual"])) for row in rows]
                        self.assertAlmostEqual(sum(errors) / len(errors), report["horizons"][str(minutes)][split][metric]["ridge"]["mae"])
                        trend_errors = [abs(float(row["linear_trend"]) - float(row["actual"])) for row in rows]
                        self.assertAlmostEqual(
                            sum(trend_errors) / len(trend_errors),
                            report["horizons"][str(minutes)][split][metric]["linear_trend"]["mae"],
                        )
            with patch.object(training, "train_horizon", side_effect=AssertionError("aucun nouvel entrainement attendu")):
                again, same, reused = training.perform_training(inputs, root / "models")
            self.assertTrue(reused)
            self.assertEqual(report, again)
            self.assertEqual(directory, same)

    def test_corrupted_existing_model_is_preserved_and_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = training.read_inputs(make_features(root))
            _, directory, _ = training.perform_training(inputs, root / "models")
            path = directory / "models.json"
            path.write_text(path.read_text() + "corrompu")
            before = path.read_bytes()
            with self.assertRaisesRegex(training.TrainingError, "corrompus"):
                training.perform_training(inputs, root / "models")
            self.assertEqual(before, path.read_bytes())

    def test_failed_publication_leaves_no_partial_training_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = training.read_inputs(make_features(root))
            with patch.object(training.os, "rename", side_effect=OSError("disque indisponible")), self.assertRaises(OSError):
                training.perform_training(inputs, root / "models")
            self.assertEqual(list((root / "models").iterdir()), [])

    def test_cli_reports_experimental_status_and_reuses_saved_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = make_features(root)
            arguments = ["--features-dir", str(directory), "--output-dir", str(root / "models")]
            for expected in ("trained_experimental", "already_trained_experimental"):
                output, error = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(error):
                    result = training.main(arguments)
                summary = json.loads(output.getvalue())
                self.assertEqual(result, 0)
                self.assertEqual(summary["status"], expected)
                self.assertEqual(set(summary["horizons"]), {"5", "10", "15"})
                self.assertTrue(Path(summary["report_file"]).is_file())


if __name__ == "__main__":
    unittest.main()
