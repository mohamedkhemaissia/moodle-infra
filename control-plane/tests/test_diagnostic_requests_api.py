import unittest

import httpx

from app.main import create_app
from pilotage.auth import AuthDenied
from pilotage.diagnostic_requests import (
    DiagnosticRequestConflict,
    DiagnosticRequestNotFound,
    DiagnosticRequestStorageError,
)
from test_auth_api import AccessFixture, ORIGIN, PASSWORD, TOKEN, csrf_token


class RequestFixture:
    def __init__(self):
        self.calls = []
        self.created = True
        self.error = None

    def check(self):
        pass

    def row(self, identifier="a" * 32, incident_id=7):
        return {
            "id": identifier,
            "incident_id": incident_id,
            "status": "queued",
            "requested_by": {"id": 1, "username": "hamma"},
            "created_at": "2026-10-14T10:00:00Z",
            "updated_at": "2026-10-14T10:00:00Z",
            "started_at": None,
            "finished_at": None,
            "run_id": None,
            "reason_code": None,
            "result_url": None,
        }

    def perform(self, name):
        self.calls.append(name)
        if self.error:
            raise self.error

    def listing(self, **kwargs):
        self.perform("listing")
        return {"items": [], "total": 0, "limit": kwargs["limit"], "offset": kwargs["offset"], "has_more": False}

    def detail(self, identifier):
        self.perform("detail")
        return self.row(identifier)

    def create(self, principal, incident_id, client_key):
        self.perform("create")
        self.principal = principal
        self.input = (incident_id, client_key)
        return self.row(incident_id=incident_id), self.created


class DiagnosticRequestAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth = AccessFixture()
        self.store = RequestFixture()
        self.app = create_app(
            auth_enabled=True,
            auth_store=self.auth,
            auth_origins=[ORIGIN],
            auth_secure_cookie=False,
            incidents_dir="/fixture/incidents",
            diagnostics_dir="/fixture/diagnostics",
            decisions_enabled=True,
            diagnostic_requests_enabled=True,
            diagnostic_request_store=self.store,
        )
        self.life = self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        self.addAsyncCleanup(self.life.__aexit__, None, None, None)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=ORIGIN)
        self.addAsyncCleanup(self.client.aclose)
        self.path = "/api/v1/diagnostic-requests"
        self.body = {"incident_id": 7, "client_key": "d" * 32}
        self.headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf_token(TOKEN)}

    async def login(self):
        return await self.client.post(
            "/api/v1/auth/login", headers={"Origin": ORIGIN}, json={"username": "hamma", "password": PASSWORD}
        )

    async def test_admin_can_enqueue_idempotently_and_reader_can_only_read(self):
        self.assertEqual((await self.client.get(self.path)).status_code, 401)
        await self.login()
        created = await self.client.post(self.path, headers=self.headers, json=self.body)
        self.assertEqual(created.status_code, 201)
        self.assertTrue(created.json()["created"])
        self.assertEqual(created.json()["mode"], "read_only")
        self.assertEqual(created.headers["cache-control"], "no-store")
        self.assertEqual(self.store.input, (7, "d" * 32))
        self.assertEqual(self.store.principal["user"]["id"], 1)
        self.store.created = False
        self.assertEqual((await self.client.post(self.path, headers=self.headers, json=self.body)).status_code, 200)
        self.auth.user["role"] = "reader"
        self.assertEqual((await self.client.get(self.path + "?incident_id=7")).status_code, 200)
        self.assertEqual((await self.client.post(self.path, headers=self.headers, json=self.body)).status_code, 403)

    async def test_inputs_and_errors_are_bounded_without_command_fields(self):
        await self.login()
        for body in (
            {**self.body, "command": "docker restart moodle"},
            {**self.body, "incident_id": True},
            {**self.body, "client_key": "short"},
        ):
            self.assertEqual((await self.client.post(self.path, headers=self.headers, json=body)).status_code, 422)
        self.assertEqual(self.store.calls, [])
        for error, expected in (
            (DiagnosticRequestConflict("Deja active."), 409),
            (DiagnosticRequestNotFound("Incident inconnu."), 404),
            (AuthDenied("Session revoquee."), 403),
            (DiagnosticRequestStorageError("postgres secret"), 503),
        ):
            self.store.error = error
            response = await self.client.post(self.path, headers=self.headers, json=self.body)
            self.assertEqual(response.status_code, expected)
            self.assertNotIn("postgres secret", response.text)

    async def test_detail_and_configuration_guards(self):
        await self.login()
        detail = await self.client.get(self.path + "/" + "a" * 32)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["request"]["id"], "a" * 32)
        self.assertEqual((await self.client.get(self.path + "/invalid")).status_code, 422)
        with self.assertRaises(ValueError):
            create_app(auth_enabled=False, incidents_dir="/x", diagnostics_dir="/x", decisions_enabled=True,
                       diagnostic_requests_enabled=True)


if __name__ == "__main__":
    unittest.main()
