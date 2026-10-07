import copy
import hashlib
import json
import math
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from app.anomalies import AnomalyBundle, AnomalyBundleError, QUALITY, SETTINGS_KEYS
from app.main import create_app
from app.resource_history import FEATURE_NAMES, METRICS, feature_vector
import test_forecasts as forecast_fixtures


def fixture():
    source_range = {"start": "2026-10-06T10:20:00Z", "end": "2026-10-06T19:22:30Z", "step_seconds": 30}
    periods = [{"split": name, "start": source_range["start"], "end_exclusive": source_range["end"]}
               for name in ("train", "validation", "test")]
    metadata = {"schema_version": 1, "input_sha256": "f"*64, "feature_names": list(FEATURE_NAMES),
                "step_seconds": 30, "lookback_seconds": 300, "source_context_seconds": 120,
                "window_points_including_endpoints": 11, "source_range": source_range,
                "split_periods": periods, "files": {"features.csv": {"sha256": "b"*64}},
                "summary": {"rows_by_split": {"train": 50, "validation": 10, "test": 10}}}
    tree = {"left": [1,-1,-1], "right": [2,-1,-1], "feature": [0,-2,-2],
            "threshold": [15,-2,-2], "samples": [50,49,1]}
    model = {"schema_version": 1, "model_type": "isolation_forest_numeric_trees", "input_dtype": "float32",
             "feature_names": list(FEATURE_NAMES), "max_samples": 50, "anomaly_threshold": 0.6,
             "trees": [copy.deepcopy(tree) for _ in range(100)]}
    report = {"tool_version": "0.1.0", "schema_version": 1, "source_input_sha256": "f"*64,
              "source_features_sha256": "b"*64, "environment": {"python": "3.10.12", "packages": {}},
              "forest_parameters": {"n_estimators": 100, "max_samples": 50, "random_state": 42,
                                    "threads": 1, "max_features": 1.0, "bootstrap": False},
              "threshold_calibration": {"partition": "train", "quantile": 0.98, "method": "higher", "comparison": "strictly_greater"},
              "minimum_rows": {"train": 50, "validation": 10, "test": 10},
              "operational_rule_thresholds": {METRICS[0]: 85, METRICS[1]: 90, METRICS[2]: 10},
              "status": "trained_experimental", "anomaly_threshold": 0.6,
              "created_at": "2026-10-07T08:00:00Z", "source_range": source_range, "split_periods": periods,
              "constant_training_features": [], "json_replay_max_absolute_difference": 1e-15,
              "detection_quality": dict(QUALITY), "by_split": {}, "files": {}}
    for name,count in metadata["summary"]["rows_by_split"].items():
        flags = 2 if name == "train" else 0
        report["by_split"][name] = {"rows": count, "statistical_anomalies": flags,
            "statistical_anomaly_rate_percent": 100*flags/count, "operational_rule_alerts": 0,
            "both_flags": 0, "score_min": 0.4, "score_median": 0.5, "score_max": 0.8}
    return model, report, metadata


def write_bundle(directory, model, report, metadata):
    encoded = lambda value: (json.dumps(value, allow_nan=False)+"\n").encode()
    model_bytes, metadata_bytes = encoded(model), encoded(metadata)
    report["files"] = {"model.json": {"sha256": hashlib.sha256(model_bytes).hexdigest()},
                       "scores.csv": {"sha256": "c"*64, "rows": 70}}
    report["source_metadata_sha256"] = hashlib.sha256(metadata_bytes).hexdigest()
    settings = {key:report[key] for key in SETTINGS_KEYS}
    report["input_sha256"] = hashlib.sha256(json.dumps(settings,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    for name,data in (("model.json",model_bytes),("features_metadata.json",metadata_bytes),("report.json",encoded(report))):
        (Path(directory)/name).write_bytes(data)


class AnomalyBundleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.model,self.report,self.metadata = fixture()

    def load(self):
        write_bundle(self.directory.name,self.model,self.report,self.metadata)
        return AnomalyBundle(self.directory.name)

    def rows(self,cpu=10,memory=30,disk=40):
        return [{METRICS[0]:cpu,METRICS[1]:memory,METRICS[2]:disk} for _ in range(15)]

    def test_numeric_score_strict_threshold_and_zero_cpu(self):
        bundle = self.load()
        value = bundle.evaluate(self.rows(cpu=20))
        c = 2*(math.log(49)+0.5772156649015329)-2*49/50
        self.assertAlmostEqual(value["score"],2**(-1/c),places=12)
        self.assertTrue(value["statistical_anomaly"])
        bundle.model["anomaly_threshold"] = value["score"]
        bundle.threshold = value["score"]
        self.assertFalse(bundle.evaluate(self.rows(cpu=20))["statistical_anomaly"])
        self.assertFalse(bundle.evaluate(self.rows(cpu=0))["statistical_anomaly"])

    def test_operational_rules_are_independent_and_disk_direction_is_correct(self):
        bundle = self.load()
        value = bundle.evaluate(self.rows(memory=90,disk=10))
        self.assertFalse(value["statistical_anomaly"])
        self.assertTrue(value["operational_rules"]["alert"])
        self.assertEqual([item["metric"] for item in value["operational_rules"]["reasons"]],list(METRICS[1:]))
        self.assertFalse(bundle.evaluate(self.rows())["operational_rules"]["alert"])

    def test_past_window_excludes_earlier_rows_and_matches_population_statistics(self):
        rows = self.rows(cpu=99)[:4]+[{METRICS[0]:10+i,METRICS[1]:30,METRICS[2]:40} for i in range(11)]
        values = feature_vector(rows,30,300)
        self.assertEqual(values[:7],[20,15,round(math.sqrt(10),6),10,20,10,2])
        self.assertEqual(len(values),21)
        with self.assertRaises(AnomalyBundleError):
            self.load().evaluate(rows[:2])

    def test_corrupted_model_and_metadata_fingerprints_are_rejected(self):
        for name in ("model.json","features_metadata.json"):
            self.load()
            (Path(self.directory.name)/name).write_text("{}")
            with self.assertRaises(AnomalyBundleError):
                AnomalyBundle(self.directory.name)

    def test_protocol_threshold_statistics_and_features_are_validated(self):
        changes = [lambda m,r,f:m["feature_names"].reverse(),
                   lambda m,r,f:r.__setitem__("anomaly_threshold",0.7),
                   lambda m,r,f:f.__setitem__("source_context_seconds",0),
                   lambda m,r,f:r["threshold_calibration"].__setitem__("partition","test"),
                   lambda m,r,f:r["by_split"]["train"].__setitem__("statistical_anomalies",51),
                   lambda m,r,f:r["by_split"]["train"].__setitem__("statistical_anomaly_rate_percent",10),
                   lambda m,r,f:r.__setitem__("detection_quality",{**QUALITY,"precision":1.0}),
                   lambda m,r,f:r.__setitem__("json_replay_max_absolute_difference",1e-3),
                   lambda m,r,f:r["forest_parameters"].__setitem__("max_samples",49)]
        for change in changes:
            self.model,self.report,self.metadata = fixture()
            change(self.model,self.report,self.metadata)
            with self.assertRaises(AnomalyBundleError):
                self.load()

    def test_duplicate_non_finite_missing_and_oversized_json_are_rejected(self):
        with self.assertRaises(AnomalyBundleError):
            AnomalyBundle(self.directory.name)
        for invalid in ('{"x":NaN}','{"x":1,"x":2}','[]','"x"',' '*(1024*1024+1)):
            self.load()
            (Path(self.directory.name)/"report.json").write_text(invalid)
            with self.assertRaises(AnomalyBundleError):
                AnomalyBundle(self.directory.name)

    def test_experiment_identity_and_description_do_not_claim_measured_accuracy(self):
        bundle = self.load()
        path = Path(self.directory.name)/"report.json"
        report = json.loads(path.read_bytes())
        report["input_sha256"] = "a"*64
        path.write_text(json.dumps(report))
        with self.assertRaises(AnomalyBundleError):
            AnomalyBundle(self.directory.name)
        description = bundle.description()
        self.assertEqual(description["mode"],"observation")
        self.assertEqual(description["detection_quality"],QUALITY)
        self.assertEqual(description["training_statistics"]["train"]["statistical_anomalies"],2)
        self.assertNotIn("source_features_directory",description)


class AnomalyAPITests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def client(self,handler,*,enabled=True,corrupt=False,forecasts=False):
        with tempfile.TemporaryDirectory() as directory,tempfile.TemporaryDirectory() as forecast_directory:
            write_bundle(directory,*fixture())
            if corrupt:
                (Path(directory)/"model.json").write_text("{}")
            if forecasts:
                forecast_fixtures.write_bundle(forecast_directory,*forecast_fixtures.fixture())
            application = create_app(transport=httpx.MockTransport(handler),
                forecasts_dir=forecast_directory if forecasts else "",anomalies_dir=directory if enabled else "")
            async with application.router.lifespan_context(application):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url="http://testserver") as client:
                    with patch("app.main.datetime") as clock:
                        clock.now.return_value = datetime(2026,10,7,8,30,12,tzinfo=timezone.utc)
                        yield client

    def handler(self,**kwargs):
        return forecast_fixtures.ForecastAPITests.handler(None,**kwargs)

    async def test_complete_history_scores_current_grid_with_provenance(self):
        async with self.client(self.handler()) as client:
            response = await client.get("/api/v1/anomalies/summary")
        self.assertEqual(response.status_code,200)
        data = response.json()
        self.assertEqual(data["status"],"available")
        self.assertEqual(data["required_history_seconds"],420)
        self.assertEqual(data["current_metrics"][METRICS[0]],24)
        self.assertTrue(data["anomaly"]["statistical_anomaly"])
        self.assertFalse(data["anomaly"]["operational_rules"]["alert"])
        self.assertEqual(data["mode"],"observation")
        self.assertEqual(data["detection_quality"],QUALITY)

    async def test_model_description_does_not_depend_on_prometheus(self):
        seen=[]
        async with self.client(self.handler(mode="offline",seen=seen)) as client:
            response = await client.get("/api/v1/anomalies/model")
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()["status"],"configured")
        self.assertEqual(seen,[])

    async def test_missing_down_stale_and_pre_window_gaps_do_not_produce_scores(self):
        for mode in ("missing","stale","down","nan","empty","old_context_gap"):
            async with self.client(self.handler(mode=mode)) as client:
                response = await client.get("/api/v1/anomalies/summary")
            self.assertEqual(response.status_code,200,mode)
            self.assertEqual(response.json()["reason"],"insufficient_contiguous_history",mode)
            self.assertIsNone(response.json()["anomaly"])

    async def test_live_collection_blocks_scores_when_history_is_complete(self):
        for mode in ("live_down","live_stale","live_missing"):
            async with self.client(self.handler(mode=mode)) as client:
                response = await client.get("/api/v1/anomalies/summary")
            self.assertEqual(response.json()["reason"],"linux_collection_unavailable")
            self.assertIsNone(response.json()["anomaly"])

    async def test_zero_cpu_is_valid(self):
        async with self.client(self.handler(mode="zero")) as client:
            data=(await client.get("/api/v1/anomalies/summary")).json()
        self.assertEqual(data["status"],"available")
        self.assertEqual(data["current_metrics"][METRICS[0]],0)
        self.assertFalse(data["anomaly"]["statistical_anomaly"])

    async def test_cache_rechecks_current_collection_before_reusing_score(self):
        seen,live=[],{}
        async with self.client(self.handler(seen=seen,live=live)) as client:
            first=(await client.get("/api/v1/anomalies/summary")).json()
            second=(await client.get("/api/v1/anomalies/summary")).json()
            live["stale"]=True
            blocked=(await client.get("/api/v1/anomalies/summary")).json()
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(first["anomaly"],second["anomaly"])
        self.assertEqual(blocked["status"],"unavailable")
        self.assertIsNone(blocked["anomaly"])
        self.assertEqual(len([r for r in seen if r.url.path.endswith("query_range")]),5)

    async def test_bad_history_returns_502_and_connection_failure_returns_503(self):
        for mode in ("ambiguous","invalid_json","out_of_domain","off_grid","duplicate","reordered"):
            async with self.client(self.handler(mode=mode)) as client:
                self.assertEqual((await client.get("/api/v1/anomalies/summary")).status_code,502,mode)
        for mode in ("offline","timeout","server_error"):
            async with self.client(self.handler(mode=mode)) as client:
                self.assertEqual((await client.get("/api/v1/anomalies/summary")).status_code,503,mode)

    async def test_disabled_or_invalid_anomalies_leave_health_and_metrics_available(self):
        for kwargs in ({"enabled":False},{"corrupt":True}):
            async with self.client(self.handler(),**kwargs) as client:
                for path in ("/api/v1/anomalies/summary","/api/v1/anomalies/model"):
                    self.assertEqual((await client.get(path)).status_code,503)
                self.assertEqual((await client.get("/api/v1/health")).status_code,200)
                self.assertEqual((await client.get("/api/v1/metrics/summary")).status_code,200)

    async def test_forecasts_and_anomalies_can_be_enabled_together(self):
        async with self.client(self.handler(),forecasts=True) as client:
            for path in ("/api/v1/forecasts/summary","/api/v1/anomalies/summary"):
                response=await client.get(path)
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json()["status"],"available")


if __name__=="__main__":
    unittest.main()
