from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import unittest

import httpx
from app.main import create_app
from app.auth import COOKIE
from pilotage.auth import csrf_token, AuthDenied
from pilotage.worker_auth import token_fingerprint

TOKEN = "a" * 43


class Accounts:
    def __init__(self, role):
        self.role = role
    def check(self):
        pass
    def authenticate(self, token):
        if token != TOKEN:
            raise AuthDenied("Session requise.")
        return {"user": {"id": 1, "role": self.role}, "csrf_token": csrf_token(TOKEN)}


class BackupAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "status.json"
        now = datetime.now(timezone.utc)
        point = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.document = {"schema_version": 1, "observed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "marker": {"status": "recorded", "latest": {"snapshot": "b" * 64,
                "frozen_at_utc": point, "completed_at_utc": point}},
            "policy": {"max_age_hours": 24}, "schedule": {"state": "active", "enabled": True},
            "job": {"state": "idle", "last_result": "success"}}
        self.path.write_text(json.dumps(self.document))

    async def client(self, role="reader", auth=True, path=None, worker=False):
        kwargs = {}
        if worker:
            token_file = Path(self.temporary.name) / "worker.sha256"
            token_file.write_text(token_fingerprint("c" * 64))
            token_file.chmod(0o600)
            kwargs["auth_worker_hash_file"] = str(token_file)
        app = create_app(auth_enabled=auth, auth_store=Accounts(role),
                         auth_origins=["http://localhost:8088"], auth_secure_cookie=False,
                         backup_status_file=str(self.path) if path is None else path, **kwargs)
        lifecycle = app.router.lifespan_context(app)
        await lifecycle.__aenter__()
        self.addAsyncCleanup(lifecycle.__aexit__, None, None, None)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 4444)),
                                   base_url="http://localhost:8088")
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_anonymous_access_is_denied(self):
        client = await self.client()
        self.assertEqual((await client.get("/api/v1/backups/summary")).status_code, 401)

    async def test_admin_and_reader_can_read_without_receiving_paths_or_secrets(self):
        for role in ("reader", "admin"):
            client = await self.client(role)
            client.cookies.set(COOKIE, TOKEN)
            response = await client.get("/api/v1/backups/summary")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["backup_state"], "recent")
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertNotIn(self.temporary.name, response.text)
            self.assertNotIn("restic_password", response.text)

    async def test_no_backup_or_restore_action_is_exposed(self):
        for role, expected in (("reader", 403), ("admin", 405)):
            client = await self.client(role)
            client.cookies.set(COOKIE, TOKEN)
            response = await client.post("/api/v1/backups/summary", json={},
                headers={"Origin": "http://localhost:8088", "X-CSRF-Token": csrf_token(TOKEN)})
            self.assertEqual(response.status_code, expected)

    async def test_missing_and_invalid_public_data_return_unknown(self):
        client = await self.client(auth=False)
        self.path.unlink()
        response = await client.get("/api/v1/backups/summary")
        self.assertEqual(response.json()["reason"], "status_file_missing")
        self.path.write_text('{"secret": "private"}')
        response = await client.get("/api/v1/backups/summary")
        self.assertEqual(response.json()["backup_state"], "unknown")
        self.assertNotIn("private", response.text)

    async def test_unconfigured_followup_is_explicit(self):
        client = await self.client(auth=False, path="")
        self.assertEqual((await client.get("/api/v1/backups/summary")).status_code, 503)

    async def test_scoped_local_worker_can_read_for_deployment_checks(self):
        client = await self.client(worker=True)
        response = await client.get("/api/v1/backups/summary", headers={"Authorization": "Bearer " + "c" * 64})
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
