"""Contrat d'acces HTTP ; les transactions des comptes sont testees sur PostgreSQL en CI."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import httpx
from app.main import create_app
from app.auth import Settings, COOKIE
from pilotage.auth import AuthDenied, AuthStorageError, AuthThrottled, AuthConflict, csrf_token, session_hash
from pilotage.worker_auth import token_fingerprint

ORIGIN = 'http://127.0.0.1:8088'
PASSWORD = 'mot-de-passe-de-test-seulement'
TOKEN = 'a' * 43
USER = {'id': 1, 'username': 'hamma', 'display_name': 'Mohamed Khemaissia',
        'role': 'admin', 'active': True, 'created_at': 1.0, 'updated_at': 1.0}


class AccessFixture:
    """Fixture du contrat, sans reproduire les requetes ni les regles du stockage."""
    def __init__(self):
        self.user = dict(USER)
        self.denied = False
        self.unavailable = False
        self.throttled = False
        self.calls = []

    def check(self):
        if self.unavailable:
            raise AuthStorageError('Stockage indisponible.')

    def authenticate(self, token):
        self.check()
        if self.denied or token != TOKEN:
            raise AuthDenied('Session expiree.')
        return {'user': dict(self.user), 'version': 1, 'session_hash': session_hash(token),
                'csrf_token': csrf_token(token)}

    def login(self, name, password):
        self.calls.append('login')
        self.check()
        if self.throttled:
            raise AuthThrottled('Trop de tentatives.')
        if name != USER['username'] or password != PASSWORD:
            raise AuthDenied('Identifiant ou mot de passe incorrect.')
        return {'user': dict(self.user), 'token': TOKEN, 'csrf_token': csrf_token(TOKEN), 'expires_at': 30000}

    def users(self, **kwargs):
        self.calls.append('users')
        return [dict(self.user)]

    def add_user(self, name, display, role, password, **kwargs):
        self.calls.append('add_user')
        return {**USER, 'id': 2, 'username': name, 'display_name': display, 'role': role}

    def update_user(self, identifier, **kwargs):
        self.calls.append('update_user')
        raise AuthConflict('Conserver au moins un administrateur actif.')

    def logout(self, principal):
        self.calls.append('logout')
        self.denied = True

    def change_password(self, principal, old, new):
        self.calls.append('change_password')
        return self.login(USER['username'], PASSWORD)

    def events(self, **kwargs):
        self.calls.append('events')
        return [{'id': 1, 'action': 'login_succeeded'}]


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = AccessFixture()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        fingerprint = Path(temporary.name) / 'worker.sha256'
        self.worker_token = 'c' * 64
        fingerprint.write_text(token_fingerprint(self.worker_token) + '\n')
        fingerprint.chmod(0o600)
        self.app = create_app(auth_enabled=True, auth_store=self.fixture, auth_origins=[ORIGIN],
                              auth_secure_cookie=False, auth_worker_hash_file=str(fingerprint))
        self.lifecycle = self.app.router.lifespan_context(self.app)
        await self.lifecycle.__aenter__()
        # Une lecture operationnelle deterministe sans contacter Prometheus.
        self.app.state.incidents = SimpleNamespace(events=lambda *args, **kwargs: {'items': []})
        self.addAsyncCleanup(self.lifecycle.__aexit__, None, None, None)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=('127.0.0.1', 4444)),
                                        base_url=ORIGIN)
        self.addAsyncCleanup(self.client.aclose)

    async def login(self):
        return await self.client.post('/api/v1/auth/login', json={'username': 'hamma', 'password': PASSWORD},
                                      headers={'Origin': ORIGIN})

    def write_headers(self):
        return {'Origin': ORIGIN, 'X-CSRF-Token': csrf_token(TOKEN)}

    async def test_login_has_http_only_cookie_and_returns_no_session_secret(self):
        response = await self.login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {'enabled', 'user', 'csrf_token', 'expires_at'})
        self.assertNotIn(PASSWORD, response.text)
        self.assertNotIn(TOKEN, response.text)
        cookie = response.headers['set-cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=strict', cookie)
        self.assertIn('Max-Age=28800', cookie)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        session = await self.client.get('/api/v1/auth/session')
        self.assertEqual(session.json()['user']['role'], 'admin')

    async def test_anonymous_access_to_data_accounts_docs_and_sessions_is_denied(self):
        for path in ('/api/v1/metrics/summary', '/api/v1/incidents', '/api/v1/users',
                     '/api/v1/auth/session', '/docs', '/openapi.json'):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 401, path)
        self.assertEqual((await self.client.get('/api/v1/health')).status_code, 200)
        self.assertEqual((await self.client.get('/api/v1/auth/status')).json(), {'enabled': True})

    async def test_reader_can_consult_but_cannot_list_create_or_edit_accounts(self):
        self.fixture.user['role'] = 'reader'
        await self.login()
        self.assertEqual((await self.client.get('/api/v1/incidents/999/events')).status_code, 200)
        for path in ('/api/v1/users', '/api/v1/auth/events'):
            self.assertEqual((await self.client.get(path)).status_code, 403)
        for method, path, body in (('POST', '/api/v1/users', {}), ('PATCH', '/api/v1/users/1', {'active': False})):
            response = await self.client.request(method, path, json=body, headers=self.write_headers())
            self.assertEqual(response.status_code, 403)
        self.assertNotIn('users', self.fixture.calls)
        self.assertNotIn('add_user', self.fixture.calls)
        self.assertNotIn('update_user', self.fixture.calls)

    async def test_reader_can_change_own_password_with_verified_session(self):
        self.fixture.user['role'] = 'reader'
        await self.login()
        response = await self.client.post('/api/v1/auth/password', headers=self.write_headers(),
            json={'old_password': PASSWORD, 'new_password': 'nouveau-mot-de-passe-test'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('change_password', self.fixture.calls)

    async def test_admin_account_creation_uses_actor_and_last_admin_conflict_is_exposed(self):
        await self.login()
        created = await self.client.post('/api/v1/users', headers=self.write_headers(),
            json={'username': 'ahmed', 'display_name': 'Ahmed', 'role': 'reader', 'password': PASSWORD})
        self.assertEqual(created.status_code, 201)
        self.assertNotIn(PASSWORD, created.text)
        self.assertEqual((await self.client.get('/api/v1/users')).status_code, 200)
        response = await self.client.patch('/api/v1/users/1', json={'active': False}, headers=self.write_headers())
        self.assertEqual(response.status_code, 409)

    async def test_cross_origin_missing_origin_csrf_and_json_are_rejected_before_mutation(self):
        await self.login()
        for headers in ({}, {'Origin': 'https://evil.example', 'X-CSRF-Token': csrf_token(TOKEN)},
                        {'Origin': ORIGIN}, {'Origin': ORIGIN, 'X-CSRF-Token': 'wrong'}):
            response = await self.client.post('/api/v1/auth/logout', json={}, headers=headers)
            self.assertEqual(response.status_code, 403)
        response = await self.client.post('/api/v1/auth/logout', content='x', headers=self.write_headers())
        self.assertEqual(response.status_code, 415)
        self.assertNotIn('logout', self.fixture.calls)

    async def test_oversized_or_invalid_inputs_do_not_reflect_the_password(self):
        for body, expected in (({'username': 'hamma', 'password': PASSWORD, 'unknown': PASSWORD}, 422),
                               ({'username': 'hamma', 'password': PASSWORD * 5}, 422),
                               ({'username': 'hamma', 'password': PASSWORD * 200}, 413)):
            response = await self.client.post('/api/v1/auth/login', json=body, headers={'Origin': ORIGIN})
            self.assertEqual(response.status_code, expected)
            self.assertNotIn(PASSWORD, response.text)
        self.assertNotIn('login', self.fixture.calls)

    async def test_wrong_password_and_limit_have_explicit_status_without_leaking_input(self):
        response = await self.client.post('/api/v1/auth/login', json={'username': 'hamma', 'password': 'wrong'},
                                          headers={'Origin': ORIGIN})
        self.assertEqual(response.status_code, 401)
        self.fixture.throttled = True
        response = await self.login()
        self.assertEqual(response.status_code, 429)
        self.assertIn('retry-after', response.headers)

    async def test_logout_and_revoked_sessions_block_subsequent_reads(self):
        await self.login()
        response = await self.client.post('/api/v1/auth/logout', json={}, headers=self.write_headers())
        self.assertEqual(response.status_code, 200)
        self.client.cookies.set(COOKIE, TOKEN)
        self.assertEqual((await self.client.get('/api/v1/incidents/999/events')).status_code, 401)

    async def test_service_identity_has_only_read_scope_and_cannot_become_a_human(self):
        headers = {'Authorization': 'Bearer ' + self.worker_token}
        self.assertEqual((await self.client.get('/api/v1/incidents/999/events', headers=headers)).status_code, 200)
        for path in ('/api/v1/users', '/api/v1/auth/session', '/api/v1/auth/events', '/docs'):
            self.assertEqual((await self.client.get(path, headers=headers)).status_code, 403)
        response = await self.client.post('/api/v1/users', json={}, headers={**headers, **self.write_headers()})
        self.assertEqual(response.status_code, 403)

    async def test_service_identity_rejects_bad_token_duplicate_headers_and_remote_peer(self):
        for headers in ({'Authorization': 'Bearer ' + 'd' * 64},
                        [('Authorization', 'Bearer ' + self.worker_token)] * 2):
            self.assertEqual((await self.client.get('/api/v1/incidents/999/events', headers=headers)).status_code, 401)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=('198.51.100.1', 4444)),
                                     base_url=ORIGIN) as remote:
            response = await remote.get('/api/v1/incidents/999/events', headers={
                'Authorization': 'Bearer ' + self.worker_token, 'X-Forwarded-For': '127.0.0.1'})
            self.assertEqual(response.status_code, 401)

    async def test_duplicate_session_cookie_is_rejected(self):
        response = await self.client.get('/api/v1/auth/session',
            headers={'Cookie': COOKIE + '=' + TOKEN + '; ' + COOKIE + '=' + TOKEN})
        self.assertEqual(response.status_code, 401)

    async def test_database_failure_does_not_open_a_legacy_or_anonymous_access(self):
        await self.login()
        self.fixture.unavailable = True
        response = await self.client.get('/api/v1/incidents/999/events')
        self.assertEqual(response.status_code, 503)
        self.assertEqual((await self.login()).status_code, 503)

    async def test_startup_rejects_missing_account_schema_when_auth_is_enabled(self):
        broken = AccessFixture()
        broken.unavailable = True
        app = create_app(auth_enabled=True, auth_store=broken, auth_origins=[ORIGIN], auth_secure_cookie=False)
        with self.assertRaises(AuthStorageError):
            async with app.router.lifespan_context(app):
                self.fail('L acces ne doit pas etre ouvert sans le stockage des comptes.')


class SettingsTests(unittest.TestCase):
    def test_remote_plain_http_bad_origins_and_insecure_production_cookie_are_rejected(self):
        for origin, secure in (('http://example.com', False), (ORIGIN, True), ('https://x.test/path', True),
                               ('https://user@x.test', True), ('https://x.test?query=x', True)):
            with self.assertRaises(ValueError):
                Settings.load(True, [origin], secure)
        settings = Settings.load(True, ['https://pilotage.example.com'], True)
        self.assertTrue(settings.secure)

    def test_secure_login_cookie_is_present_in_https_mode(self):
        async def run():
            app = create_app(auth_enabled=True, auth_store=AccessFixture(),
                              auth_origins=['https://pilotage.example.com'], auth_secure_cookie=True)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url='https://pilotage.example.com') as client:
                    result = await client.post('/api/v1/auth/login', json={'username': 'hamma', 'password': PASSWORD},
                                                headers={'Origin': 'https://pilotage.example.com'})
                    self.assertEqual(result.status_code, 200)
                    self.assertIn('Secure', result.headers['set-cookie'])
        asyncio.run(run())
