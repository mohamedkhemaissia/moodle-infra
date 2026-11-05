"""File des diagnostics guides contre le PostgreSQL 16 ephemere de CI."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import unittest

from pilotage.auth import AuthStore, AuthDenied
from pilotage.db import load_config, raw_connect
from pilotage.diagnostic_requests import (
    DiagnosticRequestConflict,
    DiagnosticRequestNotFound,
    DiagnosticRequestStore,
)
from pilotage.schemas import APP_IDS, DDL


BASE = 1791972000.0
PASSWORD = "phrase-de-test-sans-compte-reel"


class DiagnosticRequestsPostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = os.environ.get("TEST_POSTGRES_CONFIG")
        if not cls.path:
            raise RuntimeError("TEST_POSTGRES_CONFIG requis ; aucun faux backend.")
        cls.config = load_config(cls.path)

    def reset(self):
        with raw_connect(self.config) as connection:
            connection.execute("DROP SCHEMA IF EXISTS pilotage_diagnostic_requests CASCADE")
            connection.execute("DROP SCHEMA IF EXISTS pilotage_auth CASCADE")
            connection.execute("DROP SCHEMA IF EXISTS pilotage_incidents CASCADE")

    def setUp(self):
        self.reset()
        self.addCleanup(self.reset)
        with raw_connect(self.config) as connection:
            connection.execute("CREATE SCHEMA pilotage_incidents")
            connection.execute("SET search_path TO pilotage_incidents,pg_catalog")
            connection.execute(
                "CREATE TABLE schema_info(id INTEGER PRIMARY KEY,version INTEGER,application_id INTEGER)"
            )
            for statement in DDL["incidents"]:
                connection.execute(statement)
            connection.execute("INSERT INTO schema_info VALUES(1,1,%s)", (APP_IDS["incidents"],))
            for identifier, status in ((7, "open"), (8, "open"), (9, "resolved")):
                connection.execute(
                    """INSERT INTO incidents(id,signal_key,title,category,severity,status,
                       first_observed_at,opened_at,last_observed_at,resolved_at,
                       trigger_observations,unknown_observations,latest_state,context,
                       evidence_json,policy_json)
                       VALUES(%s,%s,'Fixture','resources','warning',%s,%s,%s,%s,%s,2,0,
                       'triggered','policy-v1',%s,%s)""",
                    (
                        identifier,
                        "fixture_" + str(identifier),
                        status,
                        int(BASE),
                        int(BASE),
                        int(BASE),
                        int(BASE) if status == "resolved" else None,
                        json.dumps({"fixture": True}),
                        json.dumps({"version": 1}),
                    ),
                )
        self.auth = AuthStore(database_config=self.path)
        self.auth.initialize()
        self.admin = self.auth.add_user("hamma", "Mohamed Khemaissia", "admin", PASSWORD, now=BASE)
        session = self.auth.login("hamma", PASSWORD, now=BASE)
        self.principal = {**self.auth.authenticate(session["token"], now=BASE + 1), "kind": "human"}
        self.store = DiagnosticRequestStore(database_config=self.path)
        self.assertTrue(self.store.initialize())

    def test_schema_creation_and_http_semantics_are_idempotent(self):
        self.assertFalse(self.store.initialize())
        request, created = self.store.create(self.principal, 7, "a" * 32, now=BASE + 2)
        self.assertTrue(created)
        self.assertEqual(request["status"], "queued")
        repeated, created = self.store.create(self.principal, 7, "a" * 32, now=BASE + 3)
        self.assertFalse(created)
        self.assertEqual(repeated["id"], request["id"])
        active, created = self.store.create(self.principal, 7, "b" * 32, now=BASE + 3)
        self.assertFalse(created)
        self.assertEqual(active["id"], request["id"])
        self.assertEqual(self.store.detail(request["id"]), request)
        self.assertEqual(self.store.listing(incident_id=7)["total"], 1)
        with self.assertRaises(DiagnosticRequestConflict):
            self.store.create(self.principal, 9, "c" * 32, now=BASE + 3)
        with self.assertRaises(DiagnosticRequestNotFound):
            self.store.create(self.principal, 999, "d" * 32, now=BASE + 3)

    def test_claim_requeue_and_public_result_follow_the_real_audit(self):
        request, _ = self.store.create(self.principal, 7, "a" * 32, now=BASE + 2)
        job = self.store.claim("b" * 32, now=BASE + 3)
        self.assertEqual(job["id"], request["id"])
        self.assertIsNone(self.store.detail(request["id"])["result_url"])
        self.assertEqual(self.store.requeue(job["id"], job["claim_token"], now=BASE + 4)["status"], "queued")
        job = self.store.claim("b" * 32, now=BASE + 5)
        result = self.store.finish(
            job["id"], job["claim_token"], "completed", result_available=True, now=BASE + 6
        )
        self.assertEqual(result["run_id"], request["id"])
        self.assertEqual(result["result_url"], "/api/v1/diagnostics/" + request["id"])

    def test_expired_worker_is_closed_before_the_next_request_is_claimed(self):
        first, _ = self.store.create(self.principal, 7, "a" * 32, now=BASE + 2)
        first_job = self.store.claim("b" * 32, now=BASE + 3)
        second, _ = self.store.create(self.principal, 8, "c" * 32, now=BASE + 4)
        second_job = self.store.claim("d" * 32, now=BASE + 184)
        self.assertEqual(second_job["id"], second["id"])
        interrupted = self.store.detail(first["id"])
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(interrupted["reason_code"], "worker_deadline_exceeded")
        self.assertIsNone(interrupted["run_id"])
        with self.assertRaises(DiagnosticRequestConflict):
            self.store.finish(
                first_job["id"], first_job["claim_token"], "completed",
                result_available=True, now=BASE + 185,
            )

    def test_parallel_creation_keeps_one_active_request_and_revoked_sessions_fail_closed(self):
        def create(key):
            return DiagnosticRequestStore(database_config=self.path).create(
                self.principal, 7, key * 32, now=BASE + 2
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, ("a", "b")))
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(len({request["id"] for request, _ in results}), 1)
        self.auth.add_user("secours", "Administrateur de secours", "admin", PASSWORD, now=BASE + 2)
        self.auth.update_user(self.admin["id"], active=False, now=BASE + 3)
        with self.assertRaises(AuthDenied):
            self.store.create(self.principal, 8, "c" * 32, now=BASE + 4)


if __name__ == "__main__":
    unittest.main()
