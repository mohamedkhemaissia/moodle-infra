"""Observer, historique et API verifies contre PostgreSQL 16, sans superuser."""
import asyncio
import copy
from datetime import datetime, timezone
import json
import os
import sqlite3
import unittest
from unittest.mock import patch

import httpx
from app.decisions import policy_document
from app.incidents import IncidentStore, IncidentStorageError, DEFINITIONS
from app.incident_signals import observations
from app.main import create_app
from pilotage.db import load_config, raw_connect
from pilotage.decision_journal import DecisionJournal
from pilotage.schemas import DDL, APP_IDS

BASE = 1700000010


def signals(point, *, cpu=90):
    metrics = {"collection": {"linux": {"status": "up", "scrape_age_seconds": 1},
                               "moodle_https": {"status": "up", "scrape_age_seconds": 1}},
               "metrics": {"cpu_usage_percent": cpu, "memory_usage_percent": 30, "disk_available_percent": 40,
                           "moodle_available": True, "moodle_http_status_code": 200, "moodle_probe_duration_ms": 60}}
    analysis = {"status": "available", "model_id": "e" * 64,
                "input_evaluated_at": datetime.fromtimestamp(point, timezone.utc).isoformat(),
                "anomaly": {"score": .4, "threshold": .646}}
    return observations(metrics, analysis)


class PostgreSQLDecisionJournalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config_path = os.environ['TEST_POSTGRES_CONFIG']
        cls.config = load_config(cls.config_path)
        with raw_connect(cls.config) as connection:
            if int(connection.execute('SHOW server_version_num').fetchone()[0]) // 10000 != 16:
                raise RuntimeError('PostgreSQL 16 requis.')
            if connection.execute('SELECT rolsuper FROM pg_roles WHERE rolname=current_user').fetchone()[0]:
                raise RuntimeError('Compte applicatif sans superuser requis.')

    def reset(self):
        with raw_connect(self.config) as connection:
            connection.execute('DROP SCHEMA IF EXISTS pilotage_decisions CASCADE')
            connection.execute('DROP SCHEMA IF EXISTS pilotage_incidents CASCADE')

    def setUp(self):
        self.reset()
        with raw_connect(self.config) as connection:
            connection.execute('CREATE SCHEMA pilotage_incidents')
            connection.execute('SET search_path TO pilotage_incidents,pg_catalog')
            connection.execute('CREATE TABLE schema_info(id INTEGER PRIMARY KEY,version INTEGER,application_id INTEGER)')
            for statement in DDL['incidents']:
                connection.execute(statement)
            connection.execute('INSERT INTO schema_info VALUES(1,1,%s)', (APP_IDS['incidents'],))
        self.store = IncidentStore('/unused', database_config=self.config_path, decision_journal_enabled=True)
        self.journal = self.store.decision_journal
        self.assertTrue(self.journal.initialize(now=BASE))

    def tearDown(self):
        self.reset()

    def step(self, index, *, cpu=90):
        point = BASE + index * 30
        return self.store.apply(point, signals(point, cpu=cpu), completed_at=point + 1)

    def snapshot(self):
        with raw_connect(self.config) as connection:
            return [connection.execute('SELECT row_to_json(t)::text FROM ' + table + ' t ORDER BY 1').fetchall()
                    for table in ('pilotage_decisions.entries', 'pilotage_decisions.state',
                                  'pilotage_incidents.incidents', 'pilotage_incidents.signal_state')]

    def test_idempotence_deduplication_exact_evidence_and_process_restart(self):
        self.assertFalse(self.journal.initialize(now=BASE + 1))
        self.step(0); self.step(1)
        original = self.journal.list(BASE + 32)['items'][0]
        self.assertEqual(original['status'], 'recommendation')
        self.assertFalse(original['decision']['execution_allowed'])
        self.assertEqual(original['policy'], json.loads(json.dumps(policy_document())))
        self.step(2, cpu=95)
        restarted = DecisionJournal(IncidentStore('/unused', database_config=self.config_path))
        entries = restarted.list(BASE + 62)['items']
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['observations'], 2)
        self.assertEqual(entries[0]['decision'], original['decision'])
        self.assertNotEqual(entries[0]['first_observed_at'], entries[0]['last_observed_at'])
        before = self.snapshot()
        self.assertEqual(self.step(2), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(restarted.list(BASE + 200)['coverage']['status'], 'stale')

    def test_monitoring_closed_and_new_episode_have_distinct_history(self):
        self.step(0); self.step(1); self.step(2, cpu=10); self.step(3, cpu=10); self.step(4, cpu=10)
        entries = self.journal.list(BASE + 122)['items']
        self.assertEqual([entry['status'] for entry in entries], ['closed', 'monitoring', 'recommendation'])
        self.assertEqual(entries[1]['observations'], 2)
        self.assertIsNone(entries[0]['decision']['recommended_action'])
        self.step(5); self.step(6)
        entries = self.journal.list(BASE + 182)['items']
        self.assertNotEqual(entries[0]['incident_id'], entries[1]['incident_id'])

    def test_unknown_signal_and_policy_revision_create_transitions(self):
        self.step(0); self.step(1)
        self.step(2, cpu=None)
        blocked = self.journal.list(BASE + 62)['items'][0]
        self.assertEqual(blocked['status'], 'blocked')
        self.step(3, cpu=None)
        self.assertEqual(len(self.journal.list(BASE + 92, incident_id=1)['items']), 2)
        policy = copy.deepcopy(policy_document())
        policy['sha256'] = 'f' * 64
        with patch('app.decisions.policy_document', return_value=policy):
            self.step(4, cpu=None)
        entries = self.journal.list(BASE + 122, incident_id=1)['items']
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]['policy'], json.loads(json.dumps(policy)))
        self.assertEqual(entries[0]['decision']['policy_sha256'], policy['sha256'])

    def test_failure_to_journal_rolls_back_incidents_and_collector_together(self):
        self.step(0); self.step(1)
        before = self.snapshot()
        record = self.journal.record
        def fail(*args, **kwargs):
            record(*args, **kwargs)
            raise sqlite3.DatabaseError('Echec apres les ecritures du journal')
        with patch.object(self.journal, 'record', side_effect=fail), self.assertRaises(IncidentStorageError):
            self.step(2, cpu=10)
        self.assertEqual(before, self.snapshot())
        self.step(2, cpu=10)
        self.assertEqual(self.journal.list(BASE + 62)['items'][0]['status'], 'monitoring')

    def test_existing_incidents_survive_activation_without_fabricated_past(self):
        with raw_connect(self.config) as connection:
            connection.execute('DROP SCHEMA pilotage_decisions CASCADE')
        old = IncidentStore('/unused', database_config=self.config_path)
        for index in range(2):
            point = BASE + 30 * index
            old.apply(point, signals(point), completed_at=point + 1)
        previous = old.detail(1)
        self.store.initialize()
        self.assertEqual(old.detail(1), previous)
        self.assertEqual(self.journal.list(BASE + 32)['items'], [])
        self.step(2)
        entry = self.journal.list(BASE + 62)['items'][0]
        self.assertEqual(entry['observations'], 1)
        self.assertEqual(entry['decision']['input_evaluated_at'], entry['first_observed_at'])
        self.assertFalse(self.journal.list(BASE + 62)['coverage']['backfilled'])

    def test_incompatible_schema_and_nonfinite_evidence_are_rejected_without_reinitializing(self):
        self.step(0); self.step(1)
        with raw_connect(self.config) as connection:
            connection.execute('UPDATE pilotage_decisions.schema_info SET application_id=0 WHERE id=1')
        before = self.snapshot()
        with self.assertRaises(IncidentStorageError):
            self.journal.initialize()
        self.assertEqual(self.snapshot(), before)
        with raw_connect(self.config) as connection:
            from pilotage.decision_journal import APPLICATION_ID
            connection.execute('UPDATE pilotage_decisions.schema_info SET application_id=%s WHERE id=1', (APPLICATION_ID,))
            entry = self.journal.list(BASE + 32)['items'][0]['decision']
            entry['evidence']['invalid_measure'] = float('nan')
            connection.execute('UPDATE pilotage_decisions.entries SET decision_json=%s', (json.dumps(entry),))
        with self.assertRaises(IncidentStorageError):
            self.journal.list(BASE + 32)

    def test_api_is_read_only_paginated_filtered_and_rejects_unavailable_storage(self):
        self.step(0); self.step(1); self.step(2, cpu=10); self.step(3, cpu=10); self.step(4, cpu=10)
        before = self.snapshot()
        async def run():
            with patch.dict(os.environ, PILOTAGE_DATABASE_CONFIG=self.config_path):
                app = create_app(incidents_dir='/unused', decisions_enabled=True, decision_journal_enabled=True)
                async with app.router.lifespan_context(app), httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    first = await client.get('/api/v1/decisions/history?limit=1')
                    self.assertEqual(first.status_code, 200)
                    self.assertEqual(first.headers['cache-control'], 'no-store')
                    self.assertTrue(first.json()['has_more'])
                    cursor = first.json()['next_before_id']
                    second = await client.get('/api/v1/decisions/history?limit=1&before_id=' + str(cursor))
                    self.assertLess(second.json()['items'][0]['id'], cursor)
                    selected = await client.get('/api/v1/decisions/history?status=recommendation&incident_id=1')
                    self.assertEqual(len(selected.json()['items']), 1)
                    detail = await client.get('/api/v1/decisions/history/' + str(cursor))
                    self.assertEqual(detail.json(), first.json()['items'][0])
                    self.assertEqual((await client.get('/api/v1/decisions/history/999999')).status_code, 404)
                    for query in ('limit=101', 'before_id=0', 'status=failed', 'incident_id=-1'):
                        self.assertEqual((await client.get('/api/v1/decisions/history?' + query)).status_code, 422)
                    self.assertEqual((await client.post('/api/v1/decisions/history/' + str(cursor))).status_code, 405)
                    self.assertEqual((await client.get('/api/v1/decisions/1')).status_code, 200)
                    self.assertEqual(self.snapshot(), before)
                    with raw_connect(self.config) as connection:
                        connection.execute("UPDATE pilotage_decisions.entries SET decision_json='invalid' WHERE id=%s", (cursor,))
                    self.assertEqual((await client.get('/api/v1/decisions/history')).status_code, 503)
                    self.assertEqual((await client.get('/api/v1/health')).status_code, 200)
        asyncio.run(run())

    def test_api_does_not_initialize_a_missing_journal(self):
        with raw_connect(self.config) as connection:
            connection.execute('DROP SCHEMA pilotage_decisions CASCADE')
        async def run():
            with patch.dict(os.environ, PILOTAGE_DATABASE_CONFIG=self.config_path):
                app = create_app(incidents_dir='/unused', decisions_enabled=True, decision_journal_enabled=True)
                async with app.router.lifespan_context(app), httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    self.assertEqual((await client.get('/api/v1/decisions/history')).status_code, 503)
            disabled = create_app()
            async with disabled.router.lifespan_context(disabled), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=disabled), base_url='http://test') as client:
                self.assertEqual((await client.get('/api/v1/decisions/history')).status_code, 503)
        asyncio.run(run())
        with raw_connect(self.config) as connection:
            self.assertIsNone(connection.execute("SELECT to_regnamespace('pilotage_decisions')").fetchone()[0])
