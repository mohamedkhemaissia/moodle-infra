import json
from pathlib import Path
import tempfile
import unittest

import httpx

from app.main import create_app
from app.site_operations import utcnow
from test_auth_api import AccessFixture, ORIGIN, PASSWORD, TOKEN, csrf_token


class SiteOperationsAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("operations", "requests", "processing"):
            (self.root / name).mkdir()
        self.snapshot = "1" * 64
        (self.root / "points.json").write_text(json.dumps({
            "status": "available", "generated_at": utcnow(),
            "latest_success": {"completed_at_utc": "2026-10-15T09:30:00Z"},
            "points": [{"id": self.snapshot, "created_at": "2026-10-15T09:29:00Z"}],
        }), encoding="utf-8")
        self.auth = AccessFixture()
        self.app = create_app(auth_enabled=True, auth_store=self.auth, auth_origins=[ORIGIN],
                              auth_secure_cookie=False, site_operations_enabled=True,
                              site_operations_dir=str(self.root))
        self.life = self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        self.addAsyncCleanup(self.life.__aexit__, None, None, None)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=ORIGIN)
        self.addAsyncCleanup(self.client.aclose)
        self.headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf_token(TOKEN)}
        await self.client.post("/api/v1/auth/login", headers={"Origin": ORIGIN},
                               json={"username": "hamma", "password": PASSWORD})

    async def test_admin_queues_minimal_backup_and_confirmed_restore(self):
        listing = await self.client.get("/api/v1/site-operations")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["latest_success_at"], "2026-10-15T09:30:00Z")
        created = await self.client.post("/api/v1/site-operations/backups", headers=self.headers,
                                         json={"client_key": "a" * 32})
        self.assertEqual(created.status_code, 201)
        operation = created.json()["operation"]
        self.assertTrue((self.root / "requests" / f'{operation["id"]}.json').is_file())
        operation.update(status="completed", updated_at=operation["created_at"], finished_at=operation["created_at"])
        (self.root / "operations" / f'{operation["id"]}.json').write_text(json.dumps(operation), encoding="utf-8")
        refused = await self.client.post("/api/v1/site-operations/restores", headers=self.headers,
                                         json={"snapshot_id": self.snapshot, "confirmation": "oui",
                                               "client_key": "b" * 32})
        self.assertEqual(refused.status_code, 422)
        restored = await self.client.post("/api/v1/site-operations/restores", headers=self.headers,
                                          json={"snapshot_id": self.snapshot, "confirmation": "RESTAURER",
                                                "client_key": "b" * 32})
        self.assertEqual(restored.status_code, 201)
        self.assertEqual(restored.json()["operation"]["kind"], "restore")

    async def test_reader_cannot_mutate_and_unknown_point_is_rejected(self):
        self.auth.user["role"] = "reader"
        response = await self.client.post("/api/v1/site-operations/backups", headers=self.headers,
                                          json={"client_key": "c" * 32})
        self.assertEqual(response.status_code, 403)
        self.auth.user["role"] = "admin"
        response = await self.client.post("/api/v1/site-operations/restores", headers=self.headers,
                                          json={"snapshot_id": "2" * 64, "confirmation": "RESTAURER",
                                                "client_key": "d" * 32})
        self.assertEqual(response.status_code, 409)

    async def test_configuration_requires_authentication_and_storage(self):
        with self.assertRaises(ValueError):
            create_app(auth_enabled=False, site_operations_enabled=True, site_operations_dir=str(self.root))
        with self.assertRaises(ValueError):
            create_app(auth_enabled=True, auth_store=self.auth, auth_origins=[ORIGIN],
                       auth_secure_cookie=False, site_operations_enabled=True, site_operations_dir="")


if __name__ == "__main__":
    unittest.main()
