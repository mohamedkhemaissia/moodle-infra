"""Comptes et sessions contre PostgreSQL 16 avec le compte applicatif sans superuser."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import os
import unittest

import psycopg
import httpx
from app.main import create_app
from pilotage.auth import AuthStore, AuthDenied, AuthConflict, AuthThrottled, AuthStorageError, SESSION_SECONDS, IDLE_SECONDS
from pilotage.db import load_config, raw_connect

BASE = 1700000010
PASSWORD = 'phrase-de-test-sans-compte-reel'


class PostgreSQLAccountsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config_path = os.environ.get('TEST_POSTGRES_CONFIG')
        if not cls.config_path:
            raise RuntimeError('TEST_POSTGRES_CONFIG requis, aucun faux backend.')
        cls.config = load_config(cls.config_path)
        with raw_connect(cls.config) as connection:
            if int(connection.execute('SHOW server_version_num').fetchone()[0]) // 10000 != 16:
                raise RuntimeError('PostgreSQL 16 requis.')
            if connection.execute('SELECT rolsuper FROM pg_roles WHERE rolname=current_user').fetchone()[0]:
                raise RuntimeError('Compte applicatif sans superuser requis.')

    def reset(self):
        with raw_connect(self.config) as connection:
            connection.execute('DROP SCHEMA IF EXISTS pilotage_auth CASCADE')

    def setUp(self):
        self.reset()
        self.store = AuthStore(database_config=self.config_path)
        self.assertTrue(self.store.initialize())
        self.admin = self.store.add_user('hamma', 'Mohamed Khemaissia', 'admin', PASSWORD, now=BASE)
        self.reader = self.store.add_user('ahmed', 'Ahmed', 'reader', PASSWORD, now=BASE)

    def tearDown(self):
        self.reset()

    def test_schema_is_idempotent_accounts_have_no_default_password_or_raw_session_token(self):
        self.assertFalse(self.store.initialize())
        self.assertEqual(len(self.store.users()), 2)
        session = self.store.login('hamma', PASSWORD, now=BASE + 1)
        with self.store.connection() as cursor:
            password_hash = cursor.execute("SELECT password_hash FROM users WHERE id=%s", (self.admin['id'],)).fetchone()['password_hash']
            saved = cursor.execute('SELECT * FROM sessions').fetchone()
        self.assertTrue(password_hash.startswith('$argon2id$'))
        self.assertNotIn(PASSWORD, password_hash)
        self.assertNotIn('token', saved)
        self.assertNotEqual(saved['token_hash'], session['token'])
        self.assertNotIn('password_hash', session['user'])
        principal = self.store.authenticate(session['token'], now=BASE + 2)
        self.assertEqual(principal['user']['role'], 'admin')
        self.assertEqual(principal['csrf_token'], session['csrf_token'])

    def test_duplicate_identifier_and_reader_admin_mutations_are_rejected(self):
        with self.assertRaises(AuthConflict):
            self.store.add_user('HAMMA', 'Duplicate', 'reader', PASSWORD, now=BASE)
        for operation in (lambda: self.store.users(actor_id=self.reader['id']),
                          lambda: self.store.add_user('kais', 'Kais', 'admin', PASSWORD, actor_id=self.reader['id'], now=BASE),
                          lambda: self.store.update_user(self.admin['id'], actor_id=self.reader['id'], role='reader', now=BASE),
                          lambda: self.store.events(actor_id=self.reader['id'])):
            with self.assertRaises(AuthDenied):
                operation()
        self.assertEqual(len(self.store.users()), 2)

    def test_expiration_idle_timeout_and_backward_clock_fail_closed(self):
        session = self.store.login('hamma', PASSWORD, now=BASE)
        for time in (BASE - 1, BASE + IDLE_SECONDS, BASE + SESSION_SECONDS):
            with self.assertRaises(AuthDenied):
                self.store.authenticate(session['token'], now=time)
        self.assertEqual(self.store.authenticate(session['token'], now=BASE + 1)['user']['id'], self.admin['id'])

    def test_logout_revokes_only_this_session(self):
        first = self.store.login('ahmed', PASSWORD, now=BASE)
        second = self.store.login('ahmed', PASSWORD, now=BASE + 1)
        principal = self.store.authenticate(first['token'], now=BASE + 2)
        self.store.logout(principal, now=BASE + 3)
        with self.assertRaises(AuthDenied):
            self.store.authenticate(first['token'], now=BASE + 4)
        self.assertEqual(self.store.authenticate(second['token'], now=BASE + 4)['user']['role'], 'reader')

    def test_role_change_and_deactivation_revoke_every_session(self):
        session = self.store.login('ahmed', PASSWORD, now=BASE)
        self.store.update_user(self.reader['id'], role='admin', actor_id=self.admin['id'], now=BASE + 1)
        with self.assertRaises(AuthDenied):
            self.store.authenticate(session['token'], now=BASE + 2)
        session = self.store.login('ahmed', PASSWORD, now=BASE + 3)
        self.store.update_user(self.reader['id'], active=False, actor_id=self.admin['id'], now=BASE + 4)
        with self.assertRaises(AuthDenied):
            self.store.authenticate(session['token'], now=BASE + 5)
        with self.assertRaises(AuthDenied):
            self.store.login('ahmed', PASSWORD, now=BASE + 6)

    def test_last_active_administrator_survives_concurrent_demotions(self):
        for kwargs in ({'role': 'reader'}, {'active': False}):
            with self.assertRaises(AuthConflict):
                self.store.update_user(self.admin['id'], **kwargs, now=BASE)
        second = self.store.add_user('kais', 'Kais', 'admin', PASSWORD, now=BASE)
        def demote(identifier):
            try:
                self.store.update_user(identifier, role='reader', now=BASE + 1)
                return True
            except AuthConflict:
                return False
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(demote, (self.admin['id'], second['id'])))
        self.assertEqual(sum(results), 1)
        self.assertEqual(sum(user['role'] == 'admin' and user['active'] for user in self.store.users()), 1)

    def test_administrator_cannot_disable_or_demote_self_with_another_active_admin(self):
        self.store.add_user('kais', 'Kais', 'admin', PASSWORD, now=BASE)
        session = self.store.login('hamma', PASSWORD, now=BASE)
        before = self.store.users()
        audit_before = self.store.events(actor_id=self.admin['id'])
        for kwargs in ({'role': 'reader'}, {'active': False}, {'role': 'reader', 'active': False}):
            with self.assertRaisesRegex(AuthConflict, 'propre'):
                self.store.update_user(self.admin['id'], actor_id=self.admin['id'], now=BASE + 1, **kwargs)
        self.assertEqual(self.store.users(), before)
        self.assertEqual(self.store.events(actor_id=self.admin['id']), audit_before)
        self.assertEqual(self.store.authenticate(session['token'], now=BASE + 2)['user']['role'], 'admin')
        # Une requete sans changement reste inoffensive.
        self.assertEqual(self.store.update_user(self.admin['id'], actor_id=self.admin['id'],
                         role='admin', active=True, now=BASE + 3), before[0])

    def test_password_change_revokes_old_sessions_and_preserves_only_new_cookie(self):
        first = self.store.login('ahmed', PASSWORD, now=BASE)
        other = self.store.login('ahmed', PASSWORD, now=BASE + 1)
        principal = self.store.authenticate(first['token'], now=BASE + 2)
        changed = self.store.change_password(principal, PASSWORD, 'nouvelle-phrase-secrete-de-test', now=BASE + 3)
        for old in (first, other):
            with self.assertRaises(AuthDenied):
                self.store.authenticate(old['token'], now=BASE + 4)
        self.assertEqual(self.store.authenticate(changed['token'], now=BASE + 4)['user']['id'], self.reader['id'])
        with self.assertRaises(AuthDenied):
            self.store.login('ahmed', PASSWORD, now=BASE + 5)
        self.assertEqual(self.store.login('ahmed', 'nouvelle-phrase-secrete-de-test', now=BASE + 6)['user']['id'], self.reader['id'])

    def test_local_password_recovery_revokes_existing_sessions(self):
        session = self.store.login('hamma', PASSWORD, now=BASE)
        self.store.set_password('hamma', 'mot-de-passe-recuperation-de-test', now=BASE + 1)
        with self.assertRaises(AuthDenied):
            self.store.authenticate(session['token'], now=BASE + 2)
        self.assertEqual(self.store.login('hamma', 'mot-de-passe-recuperation-de-test', now=BASE + 3)['user']['id'], self.admin['id'])

    def test_failed_login_limits_survive_a_new_store_and_are_atomic(self):
        for _ in range(5):
            with self.assertRaises(AuthDenied):
                self.store.login('ahmed', 'incorrect', now=BASE)
        another = AuthStore(database_config=self.config_path)
        with self.assertRaises(AuthThrottled):
            another.login('ahmed', PASSWORD, now=BASE + 1)
        self.assertEqual(another.login('ahmed', PASSWORD, now=BASE + 300)['user']['id'], self.reader['id'])
        def reserve(_):
            return another.reserve_login('unknown-user', BASE + 600)
        with ThreadPoolExecutor(max_workers=8) as workers:
            reserved = list(workers.map(reserve, range(12)))
        self.assertEqual(sum(reserved), 5)

    def test_account_audit_has_actions_but_no_credentials_and_does_not_create_other_schemas(self):
        with raw_connect(self.config) as connection:
            before = connection.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'pilotage_%' ORDER BY nspname").fetchall()
        session = self.store.login('hamma', PASSWORD, now=BASE)
        self.store.logout(self.store.authenticate(session['token'], now=BASE + 1), now=BASE + 2)
        events = self.store.events(actor_id=self.admin['id'])
        self.assertTrue({'user_created', 'login_succeeded', 'logout'} <= {event['action'] for event in events})
        for event in events:
            self.assertEqual(set(event), {'id', 'actor_id', 'target_id', 'action', 'observed_at'})
        with raw_connect(self.config) as connection:
            after = connection.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'pilotage_%' ORDER BY nspname").fetchall()
        self.assertEqual(before, after)

    def test_real_api_login_account_management_and_revocation_use_postgresql(self):
        async def run():
            origin = 'http://127.0.0.1:8088'
            app = create_app(auth_enabled=True, auth_store=self.store, auth_origins=[origin], auth_secure_cookie=False)
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url=origin) as admin, \
                           httpx.AsyncClient(transport=transport, base_url=origin) as reader:
                    self.assertEqual((await admin.get('/api/v1/users')).status_code, 401)
                    login = await admin.post('/api/v1/auth/login', headers={'Origin': origin},
                        json={'username': 'hamma', 'password': PASSWORD})
                    self.assertEqual(login.status_code, 200)
                    admin_headers = {'Origin': origin, 'X-CSRF-Token': login.json()['csrf_token']}
                    read_login = await reader.post('/api/v1/auth/login', headers={'Origin': origin},
                        json={'username': 'ahmed', 'password': PASSWORD})
                    self.assertEqual(read_login.status_code, 200)
                    self.assertEqual((await reader.get('/api/v1/users')).status_code, 403)
                    self.assertEqual((await admin.get('/api/v1/users')).status_code, 200)
                    created = await admin.post('/api/v1/users', headers=admin_headers,
                        json={'username': 'kais', 'display_name': 'Kais', 'role': 'reader', 'password': PASSWORD})
                    self.assertEqual(created.status_code, 201)
                    self.assertNotIn('password_hash', created.json())
                    promoted = await admin.patch('/api/v1/users/' + str(created.json()['id']),
                        headers=admin_headers, json={'role': 'admin'})
                    self.assertEqual(promoted.status_code, 200)
                    for body in ({'active': False}, {'role': 'reader'}):
                        refused = await admin.patch('/api/v1/users/' + str(self.admin['id']),
                            headers=admin_headers, json=body)
                        self.assertEqual(refused.status_code, 409)
                        self.assertIn('propre', refused.json()['detail'])
                    self.assertEqual((await admin.get('/api/v1/auth/session')).json()['user']['role'], 'admin')
                    disabled = await admin.patch('/api/v1/users/' + str(self.reader['id']),
                        headers=admin_headers, json={'active': False})
                    self.assertEqual(disabled.status_code, 200)
                    self.assertEqual((await reader.get('/api/v1/auth/session')).status_code, 401)
                    health = await admin.get('/api/v1/storage/health')
                    self.assertEqual(health.json()['stores']['accounts']['backend'], 'postgresql')
        asyncio.run(run())
