"""Integration obligatoire sur PostgreSQL 16 ; aucun faux backend ni test saute."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx
import psycopg
from psycopg import sql

from app.incidents import DEFINITIONS, IncidentStore, Observation
from app.main import create_app
from diagnostics.store import DiagnosticStore, DiagnosticStorageError
from remediation.store import RemediationStore
from pilotage.db import load_config, raw_connect
from pilotage.migrate import migrate, MigrationError
from pilotage.schemas import SCHEMAS
from pilotage.auth import AuthStore

BASE = 1700000010
RUN = '1' * 32
REMEDIATION_RUN = '2' * 32


def observations(state='clear'):
    return [Observation(name, state if name == 'cpu_high' else 'clear', {'source': "BeeCoders : l'apprenant"})
            for name in DEFINITIONS]


class PostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config_path = os.environ.get('TEST_POSTGRES_CONFIG')
        if not cls.config_path:
            raise RuntimeError('TEST_POSTGRES_CONFIG requis pour la verification PostgreSQL.')
        cls.config = load_config(cls.config_path)
        with raw_connect(cls.config) as connection:
            if int(connection.execute('SHOW server_version_num').fetchone()[0]) // 10000 != 16:
                raise RuntimeError('PostgreSQL 16 requis.')
            if connection.execute('SELECT rolsuper FROM pg_roles WHERE rolname=current_user').fetchone()[0]:
                raise RuntimeError('Les tests doivent utiliser le compte applicatif sans superuser.')

    def setUp(self):
        self.reset_database()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directories = {name: self.root / name for name in SCHEMAS}
        for directory in self.directories.values():
            directory.mkdir()
        self.incidents = IncidentStore(self.directories['incidents'], database_config='')
        self.incidents.initialize()
        self.incidents.apply(BASE, observations('triggered'), completed_at=BASE + 1)
        self.incidents.apply(BASE + 30, observations('triggered'), completed_at=BASE + 31)
        self.incidents.apply(BASE + 60, observations('unknown'), completed_at=BASE + 61)
        self.diagnostics = DiagnosticStore(self.directories['diagnostics'], database_config='')
        self.diagnostics.initialize()
        self.diagnostics.start(RUN, 1, BASE)
        self.diagnostics.finish(RUN, 'completed', BASE + 1, decision={'action_id': 'diagnose'},
                                report={'explanation': "L'apprenant, puis l'operateur"}, events=[])
        self.remediation = RemediationStore(self.directories['remediation'], database_config='')
        self.remediation.initialize()
        self.remediation.start(REMEDIATION_RUN, 1, BASE)
        self.assertIsNone(self.remediation.reserve(REMEDIATION_RUN, 1, BASE, 'abc123'))
        self.remediation.finish(REMEDIATION_RUN, 'completed', BASE + 66,
                               decision={'action_id': 'diagnose_moodle_https'}, report={'recovered': True})
        self.source_files = [self.incidents.path, self.diagnostics.path, self.remediation.path]
        self.fingerprints = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source_files}

    def tearDown(self):
        self.reset_database()

    def reset_database(self):
        with raw_connect(self.config) as connection:
            for schema in SCHEMAS.values():
                connection.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))

    def migrate(self, **kwargs):
        return migrate(self.config_path, self.directories, **kwargs)

    def pg_stores(self):
        return (IncidentStore(self.directories['incidents'], database_config=self.config_path),
                DiagnosticStore(self.directories['diagnostics'], database_config=self.config_path),
                RemediationStore(self.directories['remediation'], database_config=self.config_path))

    def assert_sources_unchanged(self):
        self.assertEqual(self.fingerprints, {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.source_files})

    def test_migration_preserves_every_historical_value_and_api_contract(self):
        before = (self.incidents.listing(), self.incidents.events(1), self.incidents.detail(1),
                  self.diagnostics.detail(RUN), self.remediation.detail(REMEDIATION_RUN),
                  self.incidents.summary(BASE + 61), self.incidents.decision_snapshot(BASE + 61))
        result = self.migrate(activation_file=self.root / 'active')
        self.assertEqual(result['status'], 'migrated')
        self.assertTrue(result['source_files_preserved'])
        self.assertEqual(result['verified_rows']['remediation']['claims'], 1)
        incidents, diagnostics, remediation = self.pg_stores()
        incidents.initialize()
        self.assertFalse(diagnostics.initialize())
        self.assertFalse(remediation.initialize())
        after = (incidents.listing(), incidents.events(1), incidents.detail(1),
                 diagnostics.detail(RUN), remediation.detail(REMEDIATION_RUN),
                 incidents.summary(BASE + 61), incidents.decision_snapshot(BASE + 61))
        self.assertEqual(before, after)
        self.assert_sources_unchanged()

    def test_account_activation_does_not_modify_migrated_incidents_or_executions(self):
        def cleanup():
            with raw_connect(self.config) as connection:
                connection.execute('DROP SCHEMA IF EXISTS pilotage_auth CASCADE')
        cleanup()
        self.addCleanup(cleanup)
        self.migrate()
        incidents, diagnostics, remediation = self.pg_stores()
        before = (incidents.listing(), incidents.events(1), incidents.summary(BASE + 61),
                  diagnostics.detail(RUN), remediation.detail(REMEDIATION_RUN))
        accounts = AuthStore(database_config=self.config_path)
        self.assertTrue(accounts.initialize())
        accounts.add_user('hamma', 'Mohamed Khemaissia', 'admin', 'phrase-de-test-sans-compte-reel', now=BASE)
        self.assertFalse(accounts.initialize())
        after = (incidents.listing(), incidents.events(1), incidents.summary(BASE + 61),
                 diagnostics.detail(RUN), remediation.detail(REMEDIATION_RUN))
        self.assertEqual(before, after)
        self.assert_sources_unchanged()

    def test_incident_confirmation_unknown_recovery_and_next_ids_work_after_migration(self):
        self.migrate()
        incidents, _, _ = self.pg_stores()
        self.assertEqual(incidents.apply(BASE + 60, observations(), completed_at=BASE + 61), 0)
        self.assertEqual(incidents.detail(1)['status'], 'open')
        for point in (BASE + 90, BASE + 120, BASE + 150):
            incidents.apply(point, observations(), completed_at=point + 1)
        self.assertEqual(incidents.detail(1)['status'], 'resolved')
        for point in (BASE + 180, BASE + 210):
            incidents.apply(point, observations('triggered'), completed_at=point + 1)
        self.assertEqual(incidents.listing(status='open')['items'][0]['id'], 2)
        self.assertEqual([e['kind'] for e in incidents.events(1)['items']],
                         ['opened', 'observation_unavailable', 'observation_resumed', 'resolved'])
        self.assert_sources_unchanged()

    def test_reservation_and_cooldown_are_preserved_and_no_duplicate_correction_is_allowed(self):
        self.migrate()
        _, diagnostics, remediation = self.pg_stores()
        identifier = '3' * 32
        remediation.start(identifier, 1, BASE + 500)
        self.assertEqual(remediation.reserve(identifier, 1, BASE + 500, 'abc123'), 'already_attempted_for_incident')
        another = '4' * 32
        remediation.start(another, 2, BASE + 100)
        self.assertEqual(remediation.reserve(another, 2, BASE + 100, 'abc123'), 'cooldown_or_clock_inconsistent')
        diagnostics.start('5' * 32, 2, BASE + 100)
        diagnostics.finish('5' * 32, 'blocked', BASE + 101)
        self.assertEqual(diagnostics.summary()['counts'], {'blocked': 1, 'completed': 1})
        self.assertEqual(remediation.detail(REMEDIATION_RUN)['action_reservation']['run_id'], REMEDIATION_RUN)

    def test_concurrent_finish_cannot_overwrite_a_terminal_result(self):
        self.migrate()
        _, diagnostics, _ = self.pg_stores()
        diagnostics.start('6' * 32, 2, BASE + 100)
        def finish():
            try:
                diagnostics.finish('6' * 32, 'completed', BASE + 101)
                return True
            except DiagnosticStorageError:
                return False
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: finish(), range(2)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(diagnostics.detail('6' * 32)['status'], 'completed')

    def test_read_only_connection_rejects_writes(self):
        self.migrate()
        _, diagnostics, _ = self.pg_stores()
        with self.assertRaises(sqlite3.DatabaseError):
            with diagnostics.connect() as connection:
                connection.execute('DELETE FROM runs')
        self.assertEqual(diagnostics.listing()['total'], 1)

    def test_repeat_migration_is_idempotent_and_preserves_new_postgresql_writes(self):
        first = self.migrate(activation_file=self.root / 'active')
        _, diagnostics, _ = self.pg_stores()
        diagnostics.start('7' * 32, 2, BASE + 200)
        diagnostics.finish('7' * 32, 'completed', BASE + 201)
        second = self.migrate(activation_file=self.root / 'active')
        self.assertEqual(second['status'], 'already_migrated')
        self.assertEqual(first['source_sha256'], second['source_sha256'])
        self.assertEqual(diagnostics.listing()['total'], 2)
        self.assert_sources_unchanged()

    def test_partial_failure_rolls_back_all_three_schemas_and_does_not_activate(self):
        with sqlite3.connect(self.remediation.path) as connection:
            connection.execute('ALTER TABLE runs ADD COLUMN unexpected TEXT')
        with self.assertRaises(MigrationError):
            self.migrate(activation_file=self.root / 'active')
        self.assertFalse((self.root / 'active').exists())
        with raw_connect(self.config) as connection:
            for schema in SCHEMAS.values():
                self.assertIsNone(connection.execute('SELECT to_regnamespace(%s)', (schema,)).fetchone()[0])

    def test_running_execution_and_existing_target_are_never_overwritten(self):
        self.diagnostics.start('8' * 32, 1, BASE + 200)
        with self.assertRaises(MigrationError):
            self.migrate()
        self.diagnostics.finish('8' * 32, 'interrupted', BASE + 201)
        with raw_connect(self.config) as connection:
            connection.execute('CREATE SCHEMA pilotage_incidents')
            connection.execute('CREATE TABLE pilotage_incidents.keep_me (id INTEGER)')
            connection.execute('INSERT INTO pilotage_incidents.keep_me VALUES(42)')
        with self.assertRaises(MigrationError):
            self.migrate()
        with raw_connect(self.config) as connection:
            self.assertEqual(connection.execute('SELECT * FROM pilotage_incidents.keep_me').fetchall(), [(42,)])

    def test_deleted_sqlite_ids_are_not_reused_after_migration(self):
        with sqlite3.connect(self.incidents.path) as connection:
            connection.execute("UPDATE sqlite_sequence SET seq=120 WHERE name='incidents'")
        self.migrate()
        incidents, _, _ = self.pg_stores()
        for point in (BASE + 90, BASE + 120, BASE + 150):
            incidents.apply(point, observations(), completed_at=point + 1)
        for point in (BASE + 180, BASE + 210):
            incidents.apply(point, observations('triggered'), completed_at=point + 1)
        self.assertEqual(incidents.listing(status='open')['items'][0]['id'], 121)

    def test_api_reads_postgresql_even_when_sqlite_sources_are_not_mounted(self):
        self.migrate()
        async def check():
            with patch.dict(os.environ, {'PILOTAGE_DATABASE_CONFIG': self.config_path}):
                app = create_app(incidents_dir=str(self.root / 'no-sqlite'),
                                 diagnostics_dir=str(self.root / 'no-sqlite'),
                                 remediation_dir=str(self.root / 'no-sqlite'))
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                        for url in ('/api/v1/incidents/1', '/api/v1/diagnostics/' + RUN,
                                    '/api/v1/remediations/' + REMEDIATION_RUN):
                            response = await client.get(url)
                            self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual((await client.get('/api/v1/incidents?status=open')).json()['total'], 1)
                        storage = await client.get('/api/v1/storage/health')
                        self.assertEqual(storage.status_code, 200, storage.text)
                        self.assertEqual({v['backend'] for v in storage.json()['stores'].values()}, {'postgresql'})
        asyncio.run(check())

    def test_observer_healthcheck_incident_summary_and_decisions_read_postgresql(self):
        from app.incident_observer import main as observer_main

        self.migrate()
        environment = {'PILOTAGE_DATABASE_CONFIG': self.config_path,
                       'INCIDENTS_DIR': str(self.root / 'no-sqlite')}
        with patch.dict(os.environ, environment), \
                patch('sys.argv', ['incident_observer', '--healthcheck']), \
                patch('app.incident_observer.time.time', return_value=BASE + 61):
            self.assertIsNone(observer_main())

        async def check():
            with patch.dict(os.environ, environment):
                app = create_app(incidents_dir=environment['INCIDENTS_DIR'], decisions_enabled=True,
                                 anomalies_dir='', diagnostics_dir='', remediation_dir='')
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                        response = await client.get('/api/v1/incidents/summary')
                        self.assertEqual(response.status_code, 200, response.text)
                        summary = response.json()
                        self.assertEqual(summary['counts'], {'open': 1, 'resolved': 0})
                        self.assertEqual(len(summary['signals']), len(DEFINITIONS))
                        for url in ('/api/v1/decisions/summary', '/api/v1/decisions/1'):
                            response = await client.get(url)
                            self.assertEqual(response.status_code, 200, response.text)
                            self.assertEqual(response.json()['mode'], 'simulation')
        asyncio.run(check())
        self.assert_sources_unchanged()


if __name__ == '__main__':
    unittest.main()
