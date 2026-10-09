from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
import httpx

from app.main import create_app
from app.auth import COOKIE
from pilotage.auth import csrf_token
from pilotage.worker_auth import token_fingerprint
from test_backups_api import Accounts, TOKEN

PATH = "/api/v1/backups/pilotage/summary"


class PilotageBackupAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "pilotage.json"
        now = datetime.now(timezone.utc)
        stamp = lambda offset: (now + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.document = {"schema_version": 1, "scope": "pilotage_postgresql", "observed_at": stamp(0),
            "marker": {"status": "recorded", "latest": {"snapshot": "b" * 64,
                "frozen_at_utc": stamp(-3600), "completed_at_utc": stamp(-3590),
                "tables": 16, "rows": 415, "repository_check": "passed"}},
            "restore": {"status": "recorded", "latest": {"run_id": "d" * 32,
                "snapshot": "b" * 64, "status": "restore_validated", "started_at": stamp(-3500),
                "completed_at": stamp(-3424), "captured_at": stamp(-3600), "duration_seconds": 76,
                "tables_verified": 16, "rows_verified": 415, "sequence_check": "passed",
                "cleanup": {"container_removed": True, "temporary_files_removed": True}}},
            "policy": {"max_age_hours": 24}, "schedule": {"state": "active", "enabled": True},
            "job": {"state": "idle", "last_result": "success"}}
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.document))

    async def client(self, role="reader", path=None, worker=False):
        kwargs = {}
        if worker:
            file = Path(self.temporary.name) / "worker.sha256"
            file.write_text(token_fingerprint("c" * 64))
            file.chmod(0o600)
            kwargs["auth_worker_hash_file"] = str(file)
        app = create_app(auth_enabled=True, auth_store=Accounts(role),
                         auth_origins=["http://localhost:8088"], auth_secure_cookie=False,
                         pilotage_backup_status_file=str(self.path) if path is None else path, **kwargs)
        lifecycle = app.router.lifespan_context(app)
        await lifecycle.__aenter__()
        self.addAsyncCleanup(lifecycle.__aexit__, None, None, None)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 4444)),
                                  base_url="http://localhost:8088")
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_sessions_are_required_and_both_roles_can_read_no_store(self):
        for role in ("admin", "reader"):
            client = await self.client(role)
            self.assertEqual((await client.get(PATH)).status_code, 401)
            client.cookies.set(COOKIE, TOKEN)
            response = await client.get(PATH)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.json()["restoration_state"], "validated")
            self.assertNotIn(self.temporary.name, response.text)

    async def test_new_snapshot_cannot_inherit_the_old_restoration_proof(self):
        self.document["marker"]["latest"]["snapshot"] = "e" * 64
        self.save()
        client = await self.client()
        client.cookies.set(COOKIE, TOKEN)
        data = (await client.get(PATH)).json()
        self.assertEqual(data["restoration_state"], "not_tested")
        self.assertFalse(data["same_snapshot"])

    async def test_no_backup_or_restore_mutation_route(self):
        for role, expected in (("reader", 403), ("admin", 405)):
            client = await self.client(role)
            client.cookies.set(COOKIE, TOKEN)
            self.assertEqual((await client.post(PATH, json={}, headers={
                "Origin": "http://localhost:8088", "X-CSRF-Token": csrf_token(TOKEN)})).status_code, expected)

    async def test_missing_corrupt_and_stale_data_do_not_report_validated(self):
        client = await self.client()
        client.cookies.set(COOKIE, TOKEN)
        self.path.unlink()
        self.assertEqual((await client.get(PATH)).json()["reason"], "status_file_missing")
        self.path.write_text('{"password":"PRIVATE"}')
        response = await client.get(PATH)
        self.assertNotIn("PRIVATE", response.text)
        self.assertEqual(response.json()["restoration_state"], "unknown")
        self.document["observed_at"] = (datetime.now(timezone.utc) - timedelta(seconds=151)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.save()
        response = await client.get(PATH)
        self.assertEqual(response.json()["reason"], "collection_stale")
        self.assertIsNone(response.json()["latest_restore"])

    async def test_unconfigured_status_is_explicit_and_local_worker_scope_is_read_only(self):
        client = await self.client(path="")
        client.cookies.set(COOKIE, TOKEN)
        self.assertEqual((await client.get(PATH)).status_code, 503)
        client = await self.client(worker=True)
        headers = {"Authorization": "Bearer " + "c" * 64}
        self.assertEqual((await client.get(PATH, headers=headers)).status_code, 200)
        self.assertEqual((await client.post(PATH, headers=headers)).status_code, 403)


if __name__ == "__main__":
    unittest.main()
