from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import httpx

from app.main import create_app
from app.auth import COOKIE
from app.application_metrics import queries
from pilotage.worker_auth import token_fingerprint, READ_PATH


def fixture_values():
    return {"php.active_processes": 0, "php.idle_processes": 2, "php.total_processes": 2,
            "php.listen_queue": 0, "php.max_children_reached": 0,
            "pg.database_connections": 3, "pg.server_connections": 4, "pg.max_connections": 100,
            "pg.commits_per_second": 1.3, "pg.rollbacks_per_second": 0, "pg.deadlocks_total": 0,
            **{prefix + suffix: value for prefix in ("php", "pg")
               for suffix, value in ((".up", 1), (".age", 2), (".service_up", 1), (".service_age", 2))}}


class ApplicationMetricsTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def client(self, overrides=None, mode=None, seen=None, enabled=True, **settings):
        values = fixture_values()
        values.update(overrides or {})
        reverse = {value: key for key, value in queries().items()}
        def handler(request):
            if mode == "offline":
                raise httpx.ConnectError("fixture", request=request)
            key = reverse[request.url.params['query']]
            if seen is not None:
                seen.append(request.url.params['time'])
            value = values[key]
            results = [] if value is None else [{"metric": {}, "value": [1, str(value)]}]
            if mode == "ambiguous":
                results *= 2
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": results}})
        app = create_app(transport=httpx.MockTransport(handler), application_metrics_enabled=enabled, **settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 4200)),
                                        base_url='http://127.0.0.1:8088') as client:
                yield client

    async def test_complete_snapshot_keeps_zero_and_uses_one_evaluation_time(self):
        seen = []
        async with self.client(seen=seen) as client:
            response = await client.get('/api/v1/metrics/applications')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        data = response.json()
        self.assertEqual(data['status'], 'available')
        self.assertEqual(data['mode'], 'read_only')
        self.assertEqual(data['services']['php_fpm']['metrics']['active_processes'], 0)
        self.assertEqual(data['services']['postgresql']['metrics']['database_connections'], 3)
        self.assertEqual(len(set(seen)), 1)
        self.assertEqual(len(seen), len(queries()))

    async def test_unreachable_service_and_failed_exporter_are_distinct(self):
        async with self.client({'php.service_up': 0}) as client:
            data = (await client.get('/api/v1/metrics/applications')).json()
        php = data['services']['php_fpm']
        self.assertEqual(php['collection']['status'], 'up')
        self.assertIs(php['service_reachable'], False)
        self.assertTrue(all(v is None for v in php['metrics'].values()))
        async with self.client({'php.up': 0}) as client:
            data = (await client.get('/api/v1/metrics/applications')).json()
        php = data['services']['php_fpm']
        self.assertEqual(php['collection']['status'], 'down')
        self.assertIsNone(php['service_reachable'])
        self.assertEqual(data['status'], 'partial')

    async def test_stale_missing_and_old_service_values_are_masked(self):
        for change in ({'php.age': 61}, {'php.age': -1}, {'php.up': None},
                       {'php.service_age': 61}, {'php.service_age': None}, {'php.service_up': None}):
            with self.subTest(change=change):
                async with self.client(change) as client:
                    response = await client.get('/api/v1/metrics/applications')
                self.assertEqual(response.status_code, 200)
                php = response.json()['services']['php_fpm']
                self.assertIsNone(php['service_reachable'])
                self.assertTrue(all(v is None for v in php['metrics'].values()))

    async def test_warmup_rates_are_partial_and_not_zero_filled(self):
        async with self.client({'pg.commits_per_second': None, 'pg.rollbacks_per_second': None}) as client:
            data = (await client.get('/api/v1/metrics/applications')).json()
        pg = data['services']['postgresql']
        self.assertEqual(pg['status'], 'partial')
        self.assertEqual(pg['metrics']['database_connections'], 3)
        self.assertIsNone(pg['metrics']['commits_per_second'])
        self.assertEqual(len(pg['unavailable_metrics']), 2)

    async def test_both_exporters_unavailable_cannot_claim_current_service_health(self):
        async with self.client({'php.up': None, 'pg.up': 0}) as client:
            data = (await client.get('/api/v1/metrics/applications')).json()
        self.assertEqual(data['status'], 'unavailable')
        self.assertTrue(all(s['service_reachable'] is None for s in data['services'].values()))

    async def test_nonfinite_missing_values_remain_unknown_and_negative_values_are_rejected(self):
        for value in ('NaN', '+Inf', '-Inf'):
            async with self.client({'pg.deadlocks_total': value}) as client:
                response = await client.get('/api/v1/metrics/applications')
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.json()['services']['postgresql']['metrics']['deadlocks_total'])
        for change in ({'php.listen_queue': -1}, {'php.service_up': 2}):
            async with self.client(change) as client:
                response = await client.get('/api/v1/metrics/applications')
            self.assertEqual(response.status_code, 502)

    async def test_disabled_offline_and_ambiguous_sources_fail_explicitly(self):
        for options, status in (({'enabled': False}, 503), ({'mode': 'offline'}, 503), ({'mode': 'ambiguous'}, 502)):
            async with self.client(**options) as client:
                response = await client.get('/api/v1/metrics/applications')
            self.assertEqual(response.status_code, status)

    async def test_route_is_readable_by_reader_and_scoped_worker_but_not_anonymous(self):
        user = {'id': 1, 'username': 'reader', 'display_name': 'Lecteur', 'role': 'reader', 'active': True}
        fixture = SimpleNamespace(check=lambda: None, authenticate=lambda _: {'user': user})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'worker.sha256'
            token = 'c' * 64
            path.write_text(token_fingerprint(token))
            path.chmod(0o600)
            async with self.client(auth_enabled=True, auth_store=fixture,
                                   auth_origins=['http://127.0.0.1:8088'], auth_secure_cookie=False,
                                   auth_worker_hash_file=str(path)) as client:
                self.assertEqual((await client.get('/api/v1/metrics/applications')).status_code, 401)
                reader = await client.get('/api/v1/metrics/applications', headers={'Cookie': COOKIE + '=test'})
                self.assertEqual(reader.status_code, 200)
                worker = await client.get('/api/v1/metrics/applications', headers={'Authorization': 'Bearer ' + token})
                self.assertEqual(worker.status_code, 200)
                self.assertNotEqual((await client.post('/api/v1/metrics/applications', headers={'Authorization': 'Bearer ' + token})).status_code, 200)
        self.assertTrue(READ_PATH.fullmatch('/api/v1/metrics/applications'))
        self.assertFalse(READ_PATH.fullmatch('/api/v1/metrics/applications/restart'))

    def test_untrusted_labels_cannot_inject_promql(self):
        for label in ('moodle"}', '../moodle', 'moodle|other'):
            with self.assertRaises(ValueError):
                queries(database=label)
