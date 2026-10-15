import tempfile
from pathlib import Path
import unittest

from ml import export_metrics
from ml import train_live


class LiveTrainingTests(unittest.TestCase):
    def test_longest_complete_window_uses_only_contiguous_real_measurements(self):
        window = export_metrics.Window(0, 180, 30)
        rows = [{"resource_metrics_complete": value} for value in (1, 1, 0, 1, 1, 1, 0)]
        result = train_live.longest_complete_window(rows, window)
        self.assertEqual((result.start, result.end, result.step), (90, 150, 30))

    def test_evidence_reports_anomaly_scope_and_limitations(self):
        source = {
            "range": {"start": "2026-10-14T08:00:00Z", "end": "2026-10-14T14:00:00Z", "step_seconds": 30},
            "summary": {"resource_complete_rows": 721},
        }
        periods = [
            {"split": "train", "start": "2026-10-14T08:00:00Z", "end_exclusive": "2026-10-14T12:00:00Z"},
            {"split": "validation", "start": "2026-10-14T12:00:00Z", "end_exclusive": "2026-10-14T13:00:00Z"},
            {"split": "test", "start": "2026-10-14T13:00:00Z", "end_exclusive": "2026-10-14T14:00:30Z"},
        ]
        features = {"summary": {"rows_by_split": {"train": 400, "validation": 70, "test": 70}},
                    "split_periods": periods}
        anomaly = {
            "anomaly_threshold": 0.61,
            "by_split": {"test": {"rows": 70, "statistical_anomaly_rate_percent": 2.5}},
            "detection_quality": {"status": "not_evaluated_no_incident_labels", "precision": None,
                                  "recall": None, "false_positive_rate": None},
            "operational_rule_thresholds": {"cpu_usage_percent": 85.0,
                                            "memory_usage_percent": 90.0,
                                            "disk_available_percent": 10.0},
        }
        evidence, markdown = train_live.evidence_document(source, features, anomaly, {})
        self.assertEqual(evidence["status"], "validated_for_observation")
        self.assertNotIn("forecasts", evidence)
        self.assertEqual(evidence["anomaly_detection"]["test_rows"], 70)
        self.assertIn("CPU utilise", markdown)
        self.assertIn("precision, rappel, fausses alertes", markdown)
        self.assertIn("pas une probabilite de panne", markdown)

    def test_write_evidence_creates_json_and_jury_summary(self):
        evidence = {"data": {"range": {"end": "2026-10-14T14:00:00Z"}}}
        with tempfile.TemporaryDirectory() as directory:
            result = train_live.write_evidence(Path(directory), evidence, "# Resume\n")
            self.assertTrue((result / "evidence.json").is_file())
            self.assertEqual((result / "jury-summary.md").read_text(encoding="utf-8"), "# Resume\n")
            self.assertFalse((result / "forecast-comparison.csv").exists())

    def test_runtime_validation_loads_the_same_assembled_api_as_deployment(self):
        repository = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            features = root / "features"
            anomalies = root / "anomalies"
            for target in (features, anomalies):
                target.mkdir()
            for target in (
                features / "metadata.json",
                anomalies / "model.json",
                anomalies / "report.json",
            ):
                target.write_text("{}\n", encoding="utf-8")

            # Les faux artefacts doivent etre refuses par le validateur, mais le
            # chargement de app.isolation_model doit d'abord reussir.
            with self.assertRaisesRegex(ValueError, "Dossier d'anomalies incomplet"):
                train_live.validate_runtime_bundle(repository, features, anomalies)


if __name__ == "__main__":
    unittest.main()
