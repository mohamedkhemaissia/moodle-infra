import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from app.main import create_app
from app.diagnostics_store import DiagnosticStore


class DiagnosticAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DiagnosticStore(self.tmp.name)
        self.store.initialize()
    def tearDown(self):
        self.tmp.cleanup()
    def client(self):
        return TestClient(create_app(diagnostics_dir=self.tmp.name))
    def test_listing_detail_and_summary_preserve_the_database(self):
        self.store.start("a" * 32, 1, 1)
        self.store.finish("a" * 32, "completed", 2, report={"checks": {"collection": "warning"}})
        before = self.store.path.read_bytes()
        with self.client() as client:
            self.assertEqual(client.get("/api/v1/health").json()["version"], "0.13.0")
            summary = client.get("/api/v1/diagnostics/summary")
            self.assertEqual(summary.status_code, 200)
            self.assertEqual(summary.json()["counts"], {"completed": 1})
            self.assertEqual(summary.headers["cache-control"], "no-store")
            self.assertEqual(client.get("/api/v1/diagnostics?incident_id=1").json()["total"], 1)
            self.assertEqual(client.get("/api/v1/diagnostics?incident_id=2").json()["total"], 0)
            detail = client.get("/api/v1/diagnostics/" + "a" * 32)
            self.assertEqual(detail.json()["report"]["checks"]["collection"], "warning")
        self.assertEqual(before, self.store.path.read_bytes())
    def test_no_execution_or_write_route_exists(self):
        with self.client() as client:
            for endpoint in ("/api/v1/diagnostics", "/api/v1/diagnostics/" + "a" * 32):
                self.assertEqual(client.post(endpoint, json={"incident_id": 1}).status_code, 405)
    def test_absent_or_incompatible_database_returns_503_without_creation(self):
        self.store.path.unlink()
        with self.client() as client:
            self.assertEqual(client.get("/api/v1/diagnostics/summary").status_code, 503)
        self.assertFalse(self.store.path.exists())
        with sqlite3.connect(self.store.path) as connection:
            connection.execute("CREATE TABLE original(value)")
        before = self.store.path.read_bytes()
        with self.client() as client:
            self.assertEqual(client.get("/api/v1/diagnostics").status_code, 503)
        self.assertEqual(before, self.store.path.read_bytes())
    def test_invalid_ids_limits_and_unknown_id(self):
        with self.client() as client:
            for path in ("/api/v1/diagnostics/invalid", "/api/v1/diagnostics?limit=101", "/api/v1/diagnostics?incident_id=0"):
                self.assertEqual(client.get(path).status_code, 422)
            self.assertEqual(client.get("/api/v1/diagnostics/" + "b" * 32).status_code, 404)
    def test_disabled_diagnostics_does_not_change_existing_endpoints(self):
        with TestClient(create_app(diagnostics_dir="")) as client:
            self.assertEqual(client.get("/api/v1/diagnostics/summary").status_code, 503)
            self.assertEqual(client.get("/api/v1/health").status_code, 200)
