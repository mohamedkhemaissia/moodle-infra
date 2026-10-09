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
ORIGIN = "http://localhost:8088"


class Accounts:
    def __init__(self, role):
        self.role = role
    def check(self):
        pass
    def authenticate(self, token):
        if token != TOKEN:
            raise AuthDenied("Session requise.")
        return {"user": {"id": 1, "role": self.role}, "csrf_token": csrf_token(TOKEN)}


class CatalogAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "catalog.json"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.value = {"schema_version": 1, "observed_at": stamp, "scopes": {
            scope: {"status": "available", "reason": None, "total": 3, "truncated": False,
                    "latest_success_presence": "unknown", "latest_success_id": None,
                    "items": [{"id": f"{n:064x}", "archived_at": stamp, "recovery_at": None,
                               "hostname": "moodleprod", "success_recorded": False} for n in (3, 2, 1)]}
            for scope in ("moodle", "pilotage")}}
        self.path.write_text(json.dumps(self.value))

    async def client(self, role="reader", auth=True, path=None, worker=False):
        kwargs = {}
        if worker:
            fingerprint = self.root / "worker.sha256"
            fingerprint.write_text(token_fingerprint("c"*64))
            fingerprint.chmod(0o600)
            kwargs["auth_worker_hash_file"] = str(fingerprint)
        application = create_app(auth_enabled=auth, auth_store=Accounts(role), auth_origins=[ORIGIN],
            auth_secure_cookie=False, backup_catalog_file=str(self.path) if path is None else path, **kwargs)
        life = application.router.lifespan_context(application)
        await life.__aenter__()
        self.addAsyncCleanup(life.__aexit__, None, None, None)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=application, client=("127.0.0.1", 4444)),
                                  base_url=ORIGIN)
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_anonymous_access_is_denied_but_admin_and_reader_can_consult(self):
        client = await self.client()
        self.assertEqual((await client.get("/api/v1/backups/catalog")).status_code, 401)
        for role in ("reader", "admin"):
            client = await self.client(role)
            client.cookies.set(COOKIE, TOKEN)
            for scope in ("moodle", "pilotage"):
                response = await client.get("/api/v1/backups/catalog", params={"scope": scope})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["scope"], scope)
                self.assertEqual(response.json()["total"], 3)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertNotIn(str(self.root), response.text)

    async def test_pagination_and_invalid_filters_are_enforced(self):
        client = await self.client(auth=False)
        response = (await client.get("/api/v1/backups/catalog?limit=1&offset=1")).json()
        self.assertEqual(response["items"][0]["id"], f"{2:064x}")
        self.assertTrue(response["has_more"])
        for query in ("scope=all", "limit=0", "limit=51", "offset=-1", "offset=201", "limit=true"):
            self.assertEqual((await client.get("/api/v1/backups/catalog?"+query)).status_code, 422, query)

    async def test_no_write_backup_or_restore_route_is_added(self):
        for role, code in (("reader", 403), ("admin", 405)):
            client = await self.client(role)
            client.cookies.set(COOKIE, TOKEN)
            response = await client.post("/api/v1/backups/catalog", json={},
                headers={"Origin": ORIGIN, "X-CSRF-Token": csrf_token(TOKEN)})
            self.assertEqual(response.status_code, code)
        client = await self.client(auth=False)
        self.assertEqual((await client.get("/api/v1/backups/catalog/restore")).status_code, 404)

    async def test_stale_missing_or_corrupt_catalogue_never_discloses_old_references(self):
        client = await self.client(auth=False)
        self.value["observed_at"] = (datetime.now(timezone.utc)-timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for scope in self.value["scopes"].values():
            for point in scope["items"]:
                point["archived_at"] = self.value["observed_at"]
        self.path.write_text(json.dumps(self.value))
        self.assertEqual((await client.get("/api/v1/backups/catalog")).json()["reason"], "collection_stale")
        self.path.write_text('{"secret":"private-password"}')
        response = await client.get("/api/v1/backups/catalog")
        self.assertEqual(response.json()["reason"], "catalog_file_invalid")
        self.assertEqual(response.json()["items"], [])
        self.assertNotIn("private-password", response.text)
        self.path.unlink()
        self.assertEqual((await client.get("/api/v1/backups/catalog")).json()["reason"], "catalog_file_missing")

    async def test_catalogue_disabled_is_distinct_from_repository_failure(self):
        client = await self.client(auth=False, path="")
        self.assertEqual((await client.get("/api/v1/backups/catalog")).status_code, 503)

    async def test_local_worker_is_limited_to_reading_catalogue(self):
        client = await self.client(worker=True)
        headers = {"Authorization": "Bearer "+"c"*64}
        response = await client.get("/api/v1/backups/catalog", headers=headers)
        self.assertEqual(response.status_code, 200)
        response = await client.post("/api/v1/backups/catalog", json={}, headers={**headers, "Origin": ORIGIN})
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
