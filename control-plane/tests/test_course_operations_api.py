import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from app.main import create_app
from app.course_operations import CourseOperationStore
from test_auth_api import AccessFixture, ORIGIN, PASSWORD, TOKEN, csrf_token


class CourseOperationsAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("operations", "requests", "backups"):
            (self.root / name).mkdir()
        (self.root / "catalog.json").write_text(json.dumps({
            "status": "available", "generated_at": "2999-01-01T00:00:00Z",
            "courses": [{"id": 2, "name": "Cours PFE", "shortname": "PFE", "category_id": 1}],
            "categories": [{"id": 1, "name": "Divers"}],
        }), encoding="utf-8")
        # Remplacer la date lointaine par l'heure courante via le contrat du module.
        from app.course_operations import utcnow
        catalog = json.loads((self.root / "catalog.json").read_text(encoding="utf-8"))
        catalog["generated_at"] = utcnow()
        (self.root / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
        self.auth = AccessFixture()
        self.app = create_app(auth_enabled=True, auth_store=self.auth, auth_origins=[ORIGIN],
                              auth_secure_cookie=False, course_operations_enabled=True,
                              course_operations_dir=str(self.root))
        self.life = self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        self.addAsyncCleanup(self.life.__aexit__, None, None, None)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=ORIGIN)
        self.addAsyncCleanup(self.client.aclose)
        self.headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf_token(TOKEN)}

    async def login(self):
        return await self.client.post("/api/v1/auth/login", headers={"Origin": ORIGIN},
                                      json={"username": "hamma", "password": PASSWORD})

    async def test_admin_queues_backup_and_restore_without_accepting_commands(self):
        self.assertEqual((await self.client.get("/api/v1/course-operations/catalog")).status_code, 401)
        await self.login()
        catalog = await self.client.get("/api/v1/course-operations/catalog")
        self.assertEqual(catalog.status_code, 200)
        self.assertEqual(catalog.json()["courses"][0]["id"], 2)
        body = {"course_id": 2, "client_key": "a" * 32}
        created = await self.client.post("/api/v1/course-operations/backups", headers=self.headers, json=body)
        self.assertEqual(created.status_code, 201)
        operation = created.json()["operation"]
        self.assertEqual(operation["status"], "queued")
        self.assertTrue((self.root / "requests" / f'{operation["id"]}.json').is_file())
        again = await self.client.post("/api/v1/course-operations/backups", headers=self.headers, json=body)
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.json()["created"])
        rejected = await self.client.post("/api/v1/course-operations/backups", headers=self.headers,
                                          json={**body, "command": "docker restart php"})
        self.assertEqual(rejected.status_code, 422)

        backup_file = self.root / "backups" / f'{operation["id"]}.mbz'
        backup_file.write_bytes(b"moodle-backup")
        operation.update(status="completed", finished_at=operation["created_at"], updated_at=operation["created_at"],
                         backup_id=operation["id"], backup={"id": operation["id"], "filename": "cours-pfe.mbz",
                         "size_bytes": backup_file.stat().st_size, "sha256": hashlib.sha256(backup_file.read_bytes()).hexdigest(),
                         "download_url": f'/api/v1/course-operations/{operation["id"]}/download'})
        (self.root / "operations" / f'{operation["id"]}.json').write_text(json.dumps(operation), encoding="utf-8")
        restore = await self.client.post("/api/v1/course-operations/restores", headers=self.headers,
            json={"backup_id": operation["id"], "category_id": 1, "client_key": "b" * 32})
        self.assertEqual(restore.status_code, 201)
        self.assertEqual(restore.json()["operation"]["kind"], "restore")
        download = await self.client.get(f'/api/v1/course-operations/{operation["id"]}/download')
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, b"moodle-backup")
        self.assertIn("cours-pfe.mbz", download.headers["content-disposition"])

    async def test_reader_can_consult_but_not_execute_and_inputs_are_bounded(self):
        await self.login()
        self.auth.user["role"] = "reader"
        self.assertEqual((await self.client.get("/api/v1/course-operations")).status_code, 200)
        response = await self.client.post("/api/v1/course-operations/backups", headers=self.headers,
            json={"course_id": 2, "client_key": "c" * 32})
        self.assertEqual(response.status_code, 403)
        self.auth.user["role"] = "admin"
        for body in ({"course_id": True, "client_key": "c" * 32},
                     {"course_id": 1, "client_key": "c" * 32},
                     {"course_id": 2, "client_key": "short"}):
            self.assertEqual((await self.client.post("/api/v1/course-operations/backups",
                                                     headers=self.headers, json=body)).status_code, 422)
        self.assertEqual((await self.client.get("/api/v1/course-operations/invalid")).status_code, 422)

    async def test_configuration_requires_authentication_and_dedicated_storage(self):
        with self.assertRaises(ValueError):
            create_app(auth_enabled=False, course_operations_enabled=True, course_operations_dir=str(self.root))
        with self.assertRaises(ValueError):
            create_app(auth_enabled=True, auth_store=self.auth, auth_origins=[ORIGIN], auth_secure_cookie=False,
                       course_operations_enabled=True, course_operations_dir="")


class CourseOperationStoreTests(unittest.TestCase):
    def test_store_rejects_symlinked_or_incoherent_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("operations", "requests", "backups"):
                (root / name).mkdir()
            store = CourseOperationStore(root)
            store.check()
            with self.assertRaises(Exception):
                store.detail("a" * 32)


if __name__ == "__main__":
    unittest.main()
