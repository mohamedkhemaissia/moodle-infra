import copy
import hashlib
import json
import math
import tempfile
import unittest
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import httpx

from app.forecasts import FEATURE_NAMES, HORIZONS, METRICS, ForecastBundle, ForecastBundleError, window_statistics
from app.main import create_app


def fixture():
    source_range = {"start": "2026-10-06T10:20:00Z", "end": "2026-10-06T19:22:30Z", "step_seconds": 30}
    metadata = {"schema_version": 1, "input_sha256": "f" * 64, "feature_names": list(FEATURE_NAMES),
                "step_seconds": 30, "lookback_seconds": 300, "source_context_seconds": 120,
                "window_points_including_endpoints": 11, "horizons_minutes": list(HORIZONS), "source_range": source_range}
    models = {"schema_version": 1, "model_type": "standard_scaler_then_multioutput_ridge",
              "input_sha256": "a" * 64, "feature_names": list(FEATURE_NAMES), "target_metrics": list(METRICS),
              "clip_range": [0, 100], "horizons": {}}
    report = {"schema_version": 1, "input_sha256": "a" * 64, "source_input_sha256": "f" * 64,
              "created_at": "2026-10-07T08:00:00Z", "status": "trained_experimental",
              "source_range": source_range, "horizons": {}, "files": {}}
    for horizon in HORIZONS:
        mean, scale = [0.0] * 21, [1.0] * 21
        mean[8], scale[8] = 2.0, 2.0
        coefficients = [[0.0] * 21 for _ in range(3)]
        coefficients[0][0], coefficients[1][8], coefficients[2][14] = 1.0, 2.0, 1.0
        preferred = {metric: "ridge" if metric == METRICS[1] else "persistence" for metric in METRICS}
        models["horizons"][str(horizon)] = {
            "horizon_minutes": horizon, "alpha": 100.0, "scaler_mean": mean, "scaler_scale": scale,
            "coefficients": coefficients, "intercept": [10.0, 3.0, 0.0], "preferred_on_validation": preferred}
        scores = {metric: {"ridge": {"mae": 1.0 if preferred[metric] == "ridge" else 3.0},
                           "persistence": {"mae": 2.0}, "preferred_on_validation": preferred[metric]}
                  for metric in METRICS}
        report["horizons"][str(horizon)] = {"selected_alpha": 100.0,
                                            "validation": copy.deepcopy(scores), "test": copy.deepcopy(scores)}
    return models, report, metadata


def write_bundle(directory, models, report, metadata):
    encoded = lambda value: (json.dumps(value, allow_nan=False) + "\n").encode()
    model_bytes, metadata_bytes = encoded(models), encoded(metadata)
    report["files"]["models.json"] = {"sha256": hashlib.sha256(model_bytes).hexdigest()}
    report["source_metadata_sha256"] = hashlib.sha256(metadata_bytes).hexdigest()
    for name, data in (("models.json", model_bytes), ("features_metadata.json", metadata_bytes), ("report.json", encoded(report))):
        (Path(directory) / name).write_bytes(data)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.models, self.report, self.metadata = fixture()

    def load(self):
        write_bundle(self.directory.name, self.models, self.report, self.metadata)
        return ForecastBundle(self.directory.name)

    def test_window_matches_population_statistics_and_time_units(self):
        values = list(range(1, 22, 2))
        self.assertEqual(window_statistics(values, 30), [21, 11, round(math.sqrt(40), 6), 1, 21, 20, 4])

    def test_scaler_coefficients_selection_and_past_window(self):
        bundle = self.load()
        rows = [{metric: 99.0 for metric in METRICS} for _ in range(4)]
        rows += [{METRICS[0]: 10 + i, METRICS[1]: 20 + i, METRICS[2]: 40 + i} for i in range(11)]
        prediction = bundle.predict(rows)["5"]
        self.assertEqual(prediction[METRICS[0]]["value"], 20)
        self.assertEqual(prediction[METRICS[0]]["ridge_value"], 30)
        self.assertEqual(prediction[METRICS[1]]["value"], 26)
        self.assertEqual(prediction[METRICS[1]]["persistence_value"], 30)
        self.assertEqual(prediction[METRICS[2]]["method"], "persistence")

    def test_clipping_matches_training_and_is_explicit(self):
        bundle = self.load()
        prediction = bundle.predict([{metric: 95.0 for metric in METRICS}] * 15)["10"][METRICS[0]]
        self.assertEqual(prediction["ridge_value"], 100)
        self.assertTrue(prediction["ridge_clipped"])
        self.assertEqual(prediction["value"], 95)

    def test_wrong_hash_and_wrong_metadata_are_rejected(self):
        self.load()
        (Path(self.directory.name) / "models.json").write_text("{}")
        with self.assertRaises(ForecastBundleError):
            ForecastBundle(self.directory.name)
        self.load()
        (Path(self.directory.name) / "features_metadata.json").write_text("{}")
        with self.assertRaises(ForecastBundleError):
            ForecastBundle(self.directory.name)

    def test_incompatible_features_dimensions_scales_and_context_are_rejected(self):
        changes = [lambda m, r, f: m["feature_names"].reverse(),
                   lambda m, r, f: m["horizons"]["5"]["scaler_scale"].__setitem__(0, 0),
                   lambda m, r, f: m["horizons"]["5"]["coefficients"].pop(),
                   lambda m, r, f: f.__setitem__("source_context_seconds", 0),
                   lambda m, r, f: f.__setitem__("step_seconds", 31),
                   lambda m, r, f: m["horizons"]["5"].__setitem__("alpha", True)]
        for change in changes:
            self.models, self.report, self.metadata = fixture()
            change(self.models, self.report, self.metadata)
            with self.assertRaises(ForecastBundleError):
                self.load()

    def test_preferred_method_cannot_be_changed_using_test_results(self):
        self.models["horizons"]["5"]["preferred_on_validation"][METRICS[0]] = "ridge"
        with self.assertRaises(ForecastBundleError):
            self.load()

    def test_non_finite_and_duplicate_json_are_rejected(self):
        self.load()
        for invalid in ('{"value": NaN}', '{"x": 1, "x": 2}'):
            (Path(self.directory.name) / "report.json").write_text(invalid)
            with self.assertRaises(ForecastBundleError):
                ForecastBundle(self.directory.name)

    def test_missing_and_oversized_artifacts_are_rejected(self):
        with self.assertRaises(ForecastBundleError):
            ForecastBundle(self.directory.name)
        self.load()
        (Path(self.directory.name) / "report.json").write_bytes(b" " * (2 * 1024 * 1024 + 1))
        with self.assertRaises(ForecastBundleError):
            ForecastBundle(self.directory.name)

    def test_overflow_and_incomplete_windows_are_rejected(self):
        self.models["horizons"]["5"]["scaler_scale"][0] = 5e-324
        bundle = self.load()
        with self.assertRaises(ForecastBundleError):
            bundle.predict([{metric: 95.0 for metric in METRICS}] * 15)
        with self.assertRaises(ForecastBundleError):
            bundle.predict([{metric: 1.0 for metric in METRICS}])


class ForecastAPITests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def client(self, handler, *, enabled=True, corrupt=False):
        with tempfile.TemporaryDirectory() as directory:
            models, report, metadata = fixture()
            write_bundle(directory, models, report, metadata)
            if corrupt:
                (Path(directory) / "models.json").write_text("{}")
            application = create_app(transport=httpx.MockTransport(handler), forecasts_dir=directory if enabled else "")
            async with application.router.lifespan_context(application):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://testserver") as client:
                    with patch("app.main.datetime") as clock:
                        clock.now.return_value = datetime(2026, 10, 7, 8, 30, 12, tzinfo=timezone.utc)
                        yield client

    def handler(self, mode=None, seen=None, live=None):
        def handle(request):
            if seen is not None:
                seen.append(request)
            if mode == "offline":
                raise httpx.ConnectError("Simulation", request=request)
            if mode == "timeout":
                raise httpx.ReadTimeout("Simulation", request=request)
            if mode == "server_error":
                return httpx.Response(503)
            if mode == "invalid_json":
                return httpx.Response(200, text="{invalid")
            params = request.url.params
            query = params["query"]
            key = "age" if query.startswith("time()") else "up" if query.startswith("up{") else \
                "cpu" if "node_cpu" in query else "memory" if "node_memory" in query else "disk"
            if request.url.path.endswith("/query"):
                value = 1 if key == "up" else 1
                if key == "age" and (mode == "live_stale" or live and live.get("stale")):
                    value = 61
                if key == "up" and mode == "live_down":
                    value = 0
                if mode == "live_missing":
                    result = []
                else:
                    result = [{"metric": {}, "value": [float(params["time"]), str(value)]}]
                return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": result}})
            start, end, step = (int(params[name]) for name in ("start", "end", "step"))
            samples = []
            for index, timestamp in enumerate(range(start, end + 1, step)):
                value = {"up": 1, "age": 1, "cpu": 10 + index, "memory": 30 + index / 10, "disk": 40}[key]
                if index == 0 and mode == "old_context_gap" and key == "cpu":
                    continue
                if index == 7 and mode == "missing" and key == "cpu":
                    continue
                if index == 7 and mode == "stale" and key == "age":
                    value = 61
                if index == 7 and mode == "down" and key == "up":
                    value = 0
                if index == 7 and mode == "nan" and key == "cpu":
                    value = "NaN"
                if mode == "zero" and key == "cpu":
                    value = 0
                if mode == "out_of_domain" and key == "memory":
                    value = 101
                samples.append([timestamp, str(value)])
            if mode == "off_grid" and key == "cpu":
                samples[0][0] += 1
            if mode == "reordered" and key == "cpu":
                samples.reverse()
            if mode == "duplicate" and key == "cpu":
                samples[1] = samples[0]
            if mode == "boolean_timestamp" and key == "cpu":
                samples[0][0] = True
            result = [{"metric": {}, "values": samples}]
            if mode == "ambiguous" and key == "memory":
                result *= 2
            if mode == "empty":
                result = []
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": result}})
        return handle

    async def test_complete_history_predicts_three_horizons_at_exact_times(self):
        seen = []
        async with self.client(self.handler(seen=seen)) as client:
            response = await client.get("/api/v1/forecasts/summary")
            health = await client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(health.json()["version"], "0.8.0")
        data = response.json()
        self.assertEqual(data["status"], "available")
        self.assertTrue(data["experimental"])
        self.assertEqual(data["required_history_seconds"], 420)
        self.assertEqual(data["current_metrics"][METRICS[0]], 24)
        self.assertEqual(data["forecasts"]["5"]["metrics"][METRICS[0]]["value"], 24)
        self.assertEqual(data["forecasts"]["5"]["metrics"][METRICS[1]]["value"], 31.9)
        from datetime import datetime
        origin = datetime.fromisoformat(data["input_evaluated_at"].replace("Z", "+00:00"))
        for horizon in HORIZONS:
            future = datetime.fromisoformat(data["forecasts"][str(horizon)]["target_evaluated_at"].replace("Z", "+00:00"))
            self.assertEqual((future - origin).total_seconds(), horizon * 60)
        ranges = [request for request in seen if request.url.path.endswith("query_range")]
        self.assertEqual(len(ranges), 5)
        self.assertEqual(len({tuple(request.url.params[name] for name in ("start", "end", "step")) for request in ranges}), 1)
        self.assertEqual(int(ranges[0].url.params["end"]), int(origin.timestamp()))

    async def test_missing_stale_down_nan_and_pre_window_gap_block_predictions(self):
        for mode in ("missing", "stale", "down", "nan", "empty", "old_context_gap"):
            with self.subTest(mode=mode):
                async with self.client(self.handler(mode)) as client:
                    response = await client.get("/api/v1/forecasts/summary")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "unavailable")
                self.assertEqual(response.json()["forecasts"], {})
                self.assertEqual(response.json()["reason"], "insufficient_contiguous_history")

    async def test_current_collection_can_block_otherwise_complete_history(self):
        for mode in ("live_stale", "live_down", "live_missing"):
            async with self.client(self.handler(mode)) as client:
                response = await client.get("/api/v1/forecasts/summary")
            self.assertEqual(response.json()["status"], "unavailable")
            self.assertEqual(response.json()["reason"], "linux_collection_unavailable")

    async def test_zero_cpu_remains_a_real_measurement(self):
        async with self.client(self.handler("zero")) as client:
            response = await client.get("/api/v1/forecasts/summary")
        self.assertEqual(response.json()["status"], "available")
        self.assertEqual(response.json()["forecasts"]["15"]["metrics"][METRICS[0]]["value"], 0)

    async def test_network_and_server_failures_return_503(self):
        for mode in ("offline", "timeout", "server_error"):
            async with self.client(self.handler(mode)) as client:
                response = await client.get("/api/v1/forecasts/summary")
                health = await client.get("/api/v1/health")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(health.status_code, 200)

    async def test_ambiguous_malformed_domain_and_timestamp_errors_return_502(self):
        for mode in ("ambiguous", "invalid_json", "out_of_domain", "off_grid", "reordered", "duplicate", "boolean_timestamp"):
            async with self.client(self.handler(mode)) as client:
                response = await client.get("/api/v1/forecasts/summary")
            self.assertEqual(response.status_code, 502, mode)

    async def test_disabled_or_corrupt_models_leave_health_and_summary_available(self):
        for enabled, corrupt in ((False, False), (True, True)):
            async with self.client(self.handler(), enabled=enabled, corrupt=corrupt) as client:
                response = await client.get("/api/v1/forecasts/summary")
                health = await client.get("/api/v1/health")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(health.status_code, 200)

    async def test_cache_rechecks_current_collection_and_is_bounded_to_one_time(self):
        seen, live = [], {"stale": False}
        async with self.client(self.handler(seen=seen, live=live)) as client:
            first = await client.get("/api/v1/forecasts/summary")
            second = await client.get("/api/v1/forecasts/summary")
            live["stale"] = True
            third = await client.get("/api/v1/forecasts/summary")
        self.assertEqual(first.json()["status"], "available")
        # A la frontiere de grille, un nouveau calcul est legitime.
        if first.json()["input_evaluated_at"] == second.json()["input_evaluated_at"]:
            self.assertTrue(second.json()["cached"])
            self.assertEqual(len([r for r in seen if r.url.path.endswith("query_range")]), 5)
        self.assertEqual(third.json()["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
