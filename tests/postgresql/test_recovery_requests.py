"""Transactions reelles des demandes ; PostgreSQL 16 obligatoire en CI."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
from datetime import datetime, timezone
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from backup_status.catalog import select_point
from pilotage.auth import AuthStore, AuthDenied
from pilotage.db import load_config, raw_connect
from pilotage.recovery import RecoveryStore, RecoveryConflict, RecoveryStorageError
import httpx
from app.main import create_app

BASE = 1791547200
PASSWORD = 'phrase-de-test-sans-compte-reel'
REASON = 'Verifier ce point dans un environnement isole'


class RecoveryPostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = os.environ.get('TEST_POSTGRES_CONFIG')
        if not cls.path: raise RuntimeError('TEST_POSTGRES_CONFIG requis ; aucun faux stockage.')
        cls.config = load_config(cls.path)

    def reset(self):
        with raw_connect(self.config) as connection:
            connection.execute('DROP SCHEMA IF EXISTS pilotage_recovery CASCADE')
            connection.execute('DROP SCHEMA IF EXISTS pilotage_auth CASCADE')

    def setUp(self):
        self.reset(); self.addCleanup(self.reset)
        self.auth = AuthStore(database_config=self.path); self.auth.initialize()
        self.user = self.auth.add_user('hamma', 'Hamma', 'admin', PASSWORD, now=BASE)
        self.other = self.auth.add_user('kais', 'Kais', 'admin', PASSWORD, now=BASE)
        self.reader = self.auth.add_user('ahmed', 'Ahmed', 'reader', PASSWORD, now=BASE)
        self.session = self.auth.login('hamma', PASSWORD, now=BASE)
        self.principal = {**self.auth.authenticate(self.session['token'], now=BASE+1), 'kind':'human'}
        second = self.auth.login('kais', PASSWORD, now=BASE)
        self.other_principal = {**self.auth.authenticate(second['token'], now=BASE+1), 'kind':'human'}
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.catalog = Path(self.temporary.name)/'catalog.json'
        self.point = dict(id='a'*64, archived_at='2026-10-09T11:00:00Z', recovery_at=None,
                          hostname='moodleprod', success_recorded=False)
        scope = dict(status='available', reason=None, total=1, items=[self.point], truncated=False,
                     latest_success_presence='unknown', latest_success_id=None)
        self.document = dict(schema_version=1, observed_at='2026-10-09T12:00:00Z',
                             scopes={'moodle':copy.deepcopy(scope), 'pilotage':copy.deepcopy(scope)})
        self.write_catalog()
        self.store = RecoveryStore(database_config=self.path,
            selector=lambda scope, snapshot, now: select_point(self.catalog, now, scope, snapshot))
        self.assertTrue(self.store.initialize())

    def write_catalog(self): self.catalog.write_text(json.dumps(self.document))

    def create(self, key='b'*32, **kwargs):
        return self.store.create(self.principal, scope='moodle', snapshot_id=self.point['id'],
                                 reason=REASON, client_key=key, now=BASE+2, **kwargs)

    def decide(self, row, action='approve', principal=None, now=BASE+3):
        return self.store.transition(principal or self.principal, row['id'], action=action, revision=row['revision'],
            reason=REASON, confirmed_snapshot_id=row['snapshot_id'] if action=='approve' else None, now=now)

    def test_initialize_is_idempotent_and_preserves_accounts_and_other_schemas(self):
        before = self.auth.users()
        with raw_connect(self.config) as connection:
            names = connection.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'pilotage_%' ORDER BY nspname").fetchall()
        self.assertFalse(self.store.initialize()); self.assertEqual(before, self.auth.users())
        with raw_connect(self.config) as connection:
            self.assertEqual(names, connection.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'pilotage_%' ORDER BY nspname").fetchall())

    def test_request_approval_and_cancellation_preserve_selected_point_and_audit(self):
        row, created = self.create(); self.assertTrue(created)
        self.assertEqual(row['status'], 'pending'); self.assertFalse(row['execution']['enabled'])
        approved = self.decide(row, principal=self.other_principal)
        self.assertTrue(approved['decision']['approval_valid']); self.assertEqual(approved['decision']['by']['username'], 'kais')
        cancelled = self.decide(approved, 'cancel', now=BASE+4)
        self.assertEqual(cancelled['status'], 'cancelled')
        detail = self.store.detail(row['id'], now=BASE+5)
        self.assertEqual([e['action'] for e in detail['events']], ['created','approved','cancelled'])
        self.assertEqual(detail['request']['selection']['point'], self.point)
        self.assertNotIn(self.session['token'], json.dumps(detail))
        self.assertNotIn(PASSWORD, json.dumps(detail))

    def test_idempotent_create_is_atomic_and_changed_payload_cannot_reuse_key(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: self.create(), range(2)))
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(len({row['id'] for row, _ in results}), 1)
        row = results[0][0]
        self.assertEqual(len(self.store.detail(row['id'], now=BASE+3)['events']), 1)
        with self.assertRaises(RecoveryConflict):
            self.store.create(self.principal, scope='pilotage', snapshot_id=self.point['id'], reason=REASON,
                              client_key='b'*32, now=BASE+3)

    def test_concurrent_approval_and_rejection_yield_one_decision_without_lost_audit(self):
        row, _ = self.create()
        def decide(action):
            try: return self.decide(row, action)['status']
            except RecoveryConflict: return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(decide, ('approve','reject')))
        self.assertEqual(results.count('conflict'), 1)
        self.assertEqual(len(self.store.detail(row['id'], now=BASE+4)['events']), 2)

    def test_retrying_same_decision_does_not_renew_approval_or_duplicate_event(self):
        row, _ = self.create(); first = self.decide(row)
        second = self.decide(row, now=BASE+4)
        self.assertEqual(first['decision']['valid_until'], second['decision']['valid_until'])
        self.assertEqual(len(self.store.detail(row['id'], now=BASE+4)['events']), 2)
        expired = self.store.detail(row['id'], now=BASE+903)['request']
        self.assertFalse(expired['decision']['approval_valid'])
        self.assertEqual(expired['execution']['status'], 'not_dispatched')

    def test_scope_removed_point_and_stale_catalog_block_approval_without_mutation(self):
        row, _ = self.create()
        for kind in ('stale', 'removed', 'changed_origin'):
            original = copy.deepcopy(self.document)
            if kind == 'stale': self.document['observed_at'] = '2026-10-09T11:30:00Z'
            elif kind == 'removed':
                self.document['scopes']['moodle'].update(total=0, items=[])
            else: self.document['scopes']['moodle']['items'][0]['hostname'] = 'foreignhost'
            self.write_catalog()
            with self.subTest(kind=kind), self.assertRaises(RecoveryConflict): self.decide(row)
            self.assertEqual(self.store.detail(row['id'], now=BASE+4)['request']['status'], 'pending')
            self.document = original; self.write_catalog()
        self.assertEqual(len(self.store.detail(row['id'], now=BASE+4)['events']), 1)

    def test_missing_catalog_blocks_create_and_unrelated_scope_failure_does_not(self):
        self.catalog.unlink()
        with self.assertRaises(RecoveryConflict): self.create()
        self.document['scopes']['pilotage'] = dict(status='unavailable', reason='repository_unavailable',
            total=None, items=[], truncated=False, latest_success_presence='unknown', latest_success_id=None)
        self.write_catalog(); self.assertTrue(self.create()[1])
        self.assertEqual(self.store.listing(now=BASE+3)['total'], 1)

    def test_revoked_demoted_expired_or_reader_session_cannot_write_after_middleware_check(self):
        self.auth.update_user(self.user['id'], role='reader', now=BASE+2)
        with self.assertRaises(AuthDenied): self.create()
        self.assertEqual(self.store.listing(now=BASE+3)['total'], 0)
        session = self.auth.login('ahmed', PASSWORD, now=BASE+3)
        reader = {**self.auth.authenticate(session['token'], now=BASE+4), 'kind':'human'}
        with self.assertRaises(AuthDenied):
            self.store.create(reader, scope='moodle', snapshot_id=self.point['id'], reason=REASON,
                              client_key='d'*32, now=BASE+5)
        with self.assertRaises(AuthDenied):
            self.store.create(self.other_principal, scope='moodle', snapshot_id=self.point['id'], reason=REASON,
                              client_key='d'*32, now=BASE+1801)

    def test_full_confirmation_stale_revision_and_clock_rollback_cannot_change_request(self):
        row, _ = self.create()
        with self.assertRaises(RecoveryConflict):
            self.store.transition(self.principal, row['id'], action='approve', revision=1, reason=REASON,
                                  confirmed_snapshot_id='f'*64, now=BASE+3)
        approved = self.decide(row)
        with self.assertRaises(RecoveryConflict): self.decide(row, 'reject', now=BASE+4)
        with self.assertRaises(RecoveryConflict): self.decide(approved, 'cancel', now=BASE+2)
        self.assertEqual(len(self.store.detail(row['id'], now=BASE+4)['events']), 2)

    def test_event_failure_rolls_back_the_request_in_the_same_transaction(self):
        with raw_connect(self.config) as connection:
            connection.execute("CREATE FUNCTION pilotage_recovery.fail_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture failure'; END $$")
            connection.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON pilotage_recovery.events FOR EACH ROW EXECUTE FUNCTION pilotage_recovery.fail_audit()")
        with self.assertRaises(RecoveryStorageError): self.create()
        self.assertEqual(self.store.listing(now=BASE+3)['total'], 0)

    def test_real_api_persists_creation_approval_and_reader_history_in_postgresql(self):
        instant = datetime.fromtimestamp(int(time.time()),timezone.utc)
        self.document['observed_at'] = instant.isoformat(timespec='seconds').replace('+00:00','Z')
        for scope in self.document['scopes'].values():
            scope['items'][0]['archived_at'] = datetime.fromtimestamp(int(time.time())-3600,timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
        self.write_catalog()
        async def run():
            origin='http://127.0.0.1:8088'
            app=create_app(auth_enabled=True,auth_store=self.auth,auth_origins=[origin],auth_secure_cookie=False,
                backup_catalog_file=str(self.catalog),recovery_requests_enabled=True,recovery_store=self.store)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=origin) as admin, \
                           httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=origin) as reader:
                    login=await admin.post('/api/v1/auth/login',headers={'Origin':origin},json={'username':'hamma','password':PASSWORD})
                    self.assertEqual(login.status_code,200)
                    headers={'Origin':origin,'X-CSRF-Token':login.json()['csrf_token']}
                    root='/api/v1/recovery/requests'
                    created=await admin.post(root,headers=headers,json=dict(scope='moodle',snapshot_id=self.point['id'],reason=REASON,client_key='f'*32))
                    self.assertEqual(created.status_code,201,created.text)
                    row=created.json()['request']
                    approved=await admin.post(root+'/'+row['id']+'/approve',headers=headers,json=dict(revision=1,reason=REASON,confirmed_snapshot_id=row['snapshot_id']))
                    self.assertEqual(approved.status_code,200,approved.text)
                    self.assertFalse(approved.json()['execution_enabled'])
                    read_login=await reader.post('/api/v1/auth/login',headers={'Origin':origin},json={'username':'ahmed','password':PASSWORD})
                    self.assertEqual(read_login.status_code,200)
                    detail=await reader.get(root+'/'+row['id']); self.assertEqual(detail.status_code,200)
                    self.assertEqual([e['action'] for e in detail.json()['events']],['created','approved'])
                    denied=await reader.post(root+'/'+row['id']+'/cancel',headers={'Origin':origin,'X-CSRF-Token':read_login.json()['csrf_token']},json=dict(revision=2,reason=REASON))
                    self.assertEqual(denied.status_code,403)
                    self.assertEqual((await admin.get('/api/v1/storage/health')).json()['stores']['recovery_requests']['backend'],'postgresql')
        asyncio.run(run())


if __name__ == '__main__': unittest.main()
