from contextlib import asynccontextmanager
import unittest

import httpx

from app.main import create_app


class APITests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def client(self, handler):
        application = create_app(transport=httpx.MockTransport(handler))
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application),
                base_url="http://testserver",
            ) as client:
                yield client

    def mock(self, overrides=None, mode=None, seen=None):
        values = {
            "cpu": 7.25,
            "memory": 18.5,
            "disk": 43.2,
            "available": 1,
            "duration": 74,
            "http": 200,
            "linux_up": 1,
            "linux_age": 2,
            "moodle_https_up": 1,
            "moodle_https_age": 3,
        }
        values.update(overrides or {})

        def handler(request):
            if seen is not None:
                seen.append(request.url.params["time"])
            if mode == "offline":
                raise httpx.ConnectError("Panne simulee", request=request)
            if mode == "timeout":
                raise httpx.ReadTimeout("Delai simule", request=request)
            if mode == "server_error":
                return httpx.Response(503)
            if mode == "bad_json":
                return httpx.Response(200, text="{broken")
            if mode == "query_error":
                return httpx.Response(200, json={"status": "error", "error": "bad query"})
            query = request.url.params["query"]
            job = "linux" if 'job="linux"' in query else "moodle_https"
            if query.startswith("time() - timestamp("):
                key = f"{job}_age"
            elif query.startswith("up{"):
                key = f"{job}_up"
            elif "node_cpu" in query:
                key = "cpu"
            elif "node_memory" in query:
                key = "memory"
            elif "node_filesystem" in query:
                key = "disk"
            elif "probe_success" in query:
                key = "available"
            elif "probe_duration" in query:
                key = "duration"
            else:
                key = "http"
            value = values[key]
            result = [] if value is None else [{"metric": {}, "value": [1, str(value)]}]
            if mode == "ambiguous" and key == "memory":
                result = result * 2
            return httpx.Response(
                200, json={"status": "success", "data": {"resultType": "vector", "result": result}}
            )

        return handler

    async def test_complete_snapshot_and_common_evaluation_time(self):
        seen = []
        async with self.client(self.mock(seen=seen)) as client:
            response = await client.get("/api/v1/metrics/summary")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["metrics"]["cpu_usage_percent"], 7.25)
        self.assertEqual(data["metrics"]["memory_usage_percent"], 18.5)
        self.assertEqual(data["metrics"]["disk_available_percent"], 43.2)
        self.assertEqual(data["metrics"]["moodle_probe_duration_ms"], 74)
        self.assertEqual(data["metrics"]["moodle_http_status_code"], 200)
        self.assertIs(data["metrics"]["moodle_available"], True)
        self.assertEqual(data["unavailable_metrics"], [])
        self.assertEqual(len(seen), 10)
        self.assertEqual(len(set(seen)), 1)

    async def test_zero_cpu_and_moodle_failure_are_real_values(self):
        async with self.client(self.mock({"cpu": 0, "available": 0, "http": 0})) as client:
            data = (await client.get("/api/v1/metrics/summary")).json()
        self.assertEqual(data["metrics"]["cpu_usage_percent"], 0)
        self.assertIs(data["metrics"]["moodle_available"], False)
        self.assertEqual(data["metrics"]["moodle_http_status_code"], 0)
        self.assertEqual(data["collection"]["moodle_https"]["status"], "up")
        self.assertEqual(data["unavailable_metrics"], [])

    async def test_missing_cpu_is_null(self):
        async with self.client(self.mock({"cpu": None})) as client:
            data = (await client.get("/api/v1/metrics/summary")).json()
        self.assertIsNone(data["metrics"]["cpu_usage_percent"])
        self.assertEqual(data["unavailable_metrics"], ["cpu_usage_percent"])

    async def test_non_finite_memory_is_null(self):
        for value in ("NaN", "+Inf", "-Inf"):
            with self.subTest(value=value):
                async with self.client(self.mock({"memory": value})) as client:
                    response = await client.get("/api/v1/metrics/summary")
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.json()["metrics"]["memory_usage_percent"])

    async def test_failed_host_collection_suppresses_historical_values(self):
        async with self.client(self.mock({"linux_up": 0})) as client:
            data = (await client.get("/api/v1/metrics/summary")).json()
        self.assertEqual(data["collection"]["linux"]["status"], "down")
        self.assertIsNone(data["metrics"]["cpu_usage_percent"])
        self.assertIsNone(data["metrics"]["memory_usage_percent"])
        self.assertIsNone(data["metrics"]["disk_available_percent"])
        self.assertIs(data["metrics"]["moodle_available"], True)

    async def test_stale_collection_is_reported(self):
        async with self.client(self.mock({"linux_age": 61})) as client:
            data = (await client.get("/api/v1/metrics/summary")).json()
        self.assertEqual(data["collection"]["linux"]["status"], "stale")
        self.assertIsNone(data["metrics"]["cpu_usage_percent"])

    async def test_failed_probe_collection_cannot_report_moodle_healthy(self):
        async with self.client(self.mock({"moodle_https_up": 0})) as client:
            data = (await client.get("/api/v1/metrics/summary")).json()
        self.assertIsNone(data["metrics"]["moodle_available"])
        self.assertIsNone(data["metrics"]["moodle_probe_duration_ms"])
        self.assertIsNone(data["metrics"]["moodle_http_status_code"])

    async def test_missing_collection_age_is_reported(self):
        async with self.client(self.mock({"linux_age": None})) as client:
            data = (await client.get("/api/v1/metrics/summary")).json()
        self.assertEqual(data["collection"]["linux"]["status"], "missing")
        self.assertIsNone(data["metrics"]["cpu_usage_percent"])

    async def test_prometheus_connection_failure_returns_503(self):
        async with self.client(self.mock(mode="offline")) as client:
            response = await client.get("/api/v1/metrics/summary")
            health = await client.get("/api/v1/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(health.status_code, 200)

    async def test_prometheus_timeout_returns_503(self):
        async with self.client(self.mock(mode="timeout")) as client:
            self.assertEqual((await client.get("/api/v1/metrics/summary")).status_code, 503)

    async def test_prometheus_server_error_returns_503(self):
        async with self.client(self.mock(mode="server_error")) as client:
            self.assertEqual((await client.get("/api/v1/metrics/summary")).status_code, 503)

    async def test_malformed_response_returns_502(self):
        async with self.client(self.mock(mode="bad_json")) as client:
            self.assertEqual((await client.get("/api/v1/metrics/summary")).status_code, 502)

    async def test_prometheus_query_error_returns_502(self):
        async with self.client(self.mock(mode="query_error")) as client:
            self.assertEqual((await client.get("/api/v1/metrics/summary")).status_code, 502)

    async def test_ambiguous_series_return_502(self):
        async with self.client(self.mock(mode="ambiguous")) as client:
            self.assertEqual((await client.get("/api/v1/metrics/summary")).status_code, 502)


if __name__ == "__main__":
    unittest.main()
