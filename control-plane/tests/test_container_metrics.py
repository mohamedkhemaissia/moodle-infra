from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

import httpx

from app.main import create_app
from app.auth import COOKIE
from app.container_metrics import FIELDS, METRICS, queries, parse_targets
from pilotage.worker_auth import READ_PATH, token_fingerprint


class ContainerMetricsTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def client(self, changes=None, mode=None, seen=None, enabled=True, **settings):
        reverse = {value: key for key, value in queries().items()}
        values = {'instances': 1, 'present': 1, 'running': 1, 'paused': 0, 'health': None,
                  'stats_success': 1, 'stats_timestamp_seconds': 'recent', 'cpu_usage_percent': 0,
                  'memory_working_set_bytes': 1048576, 'memory_limit_bytes': 2097152, 'pids': 1,
                  'collection_success': 1, 'inventory_timestamp_seconds': 'recent', 'textfile_error': 0,
                  'linux_up': 1, 'linux_age': 2}
        values.update(changes or {})
        def handler(request):
            if mode == 'offline':
                raise httpx.ConnectError('fixture', request=request)
            field = reverse[request.url.params['query']]
            evaluated = float(request.url.params['time'])
            if seen is not None:
                seen.append(evaluated)
            value = values[field]
            if value in ('recent', 'old', 'future'):
                value = evaluated - {'recent': 2, 'old': 91, 'future': -1}[value]
            rows = [] if value is None else ([{'metric': {'project': 'moodle-pilote', 'compose_service': s,
                                                         'service': 'moodle-host', 'environment': 'pfe'}, 'value': [evaluated, str(value)]}
                                            for s in ('apache', 'php', 'postgres')] if field in FIELDS else
                                           [{'metric': {}, 'value': [evaluated, str(value)]}])
            if mode == 'duplicate' and field in FIELDS:
                rows += rows[:1]
            return httpx.Response(200, json={'status': 'success', 'data': {'resultType': 'vector', 'result': rows}})
        app = create_app(transport=httpx.MockTransport(handler), container_metrics_enabled=enabled, **settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 4200)),
                                        base_url='http://127.0.0.1:8088') as client:
                yield client

    async def test_current_snapshot_keeps_zero_and_uses_one_time_for_all_queries(self):
        seen = []
        async with self.client(seen=seen) as client:
            response = await client.get('/api/v1/metrics/containers')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        data = response.json()
        self.assertEqual(data['status'], 'available')
        self.assertEqual(data['containers'][0]['metrics']['cpu_usage_percent'], 0)
        self.assertIsNone(data['containers'][0]['health'])
        self.assertEqual(len(set(seen)), 1)

    async def test_new_scrapes_cannot_refresh_an_old_timer_file(self):
        async with self.client({'inventory_timestamp_seconds': 'old'}) as client:
            data = (await client.get('/api/v1/metrics/containers')).json()
        self.assertEqual(data['collection']['status'], 'up')
        self.assertEqual(data['collector']['status'], 'stale')
        self.assertEqual(data['status'], 'unavailable')
        self.assertTrue(all(c['state'] == 'unknown' and all(v is None for v in c['metrics'].values()) for c in data['containers']))

    async def test_exporter_failure_collector_failure_and_textfile_error_hide_all_states(self):
        for change in ({'linux_up': 0}, {'linux_up': None}, {'linux_age': 61},
                       {'collection_success': 0}, {'textfile_error': 1},
                       {'inventory_timestamp_seconds': None}, {'inventory_timestamp_seconds': 'future'}):
            with self.subTest(change=change):
                async with self.client(change) as client:
                    response = await client.get('/api/v1/metrics/containers')
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual(data['status'], 'unavailable')
                self.assertTrue(all(c['state'] == 'unknown' and c['instances'] is None for c in data['containers']))

    async def test_old_failed_or_first_stats_do_not_erase_current_inventory(self):
        for change in ({'stats_timestamp_seconds': 'old'}, {'stats_timestamp_seconds': 'future'},
                       {'stats_success': 0}, {'stats_timestamp_seconds': None}):
            async with self.client(change) as client:
                data = (await client.get('/api/v1/metrics/containers')).json()
            self.assertEqual(data['containers'][0]['state'], 'running')
            self.assertTrue(all(v is None for v in data['containers'][0]['metrics'].values()))
        async with self.client({'cpu_usage_percent': None}) as client:
            data = (await client.get('/api/v1/metrics/containers')).json()
        self.assertEqual(data['containers'][0]['metrics_status'], 'partial')
        self.assertIsNone(data['containers'][0]['metrics']['cpu_usage_percent'])
        self.assertEqual(data['containers'][0]['metrics']['memory_working_set_bytes'], 1048576)

    async def test_absent_nonrunning_paused_and_scaled_are_distinct(self):
        cases = [({'instances': 0, 'present': 0}, 'missing'),
                 ({'running': 0}, 'not_running'), ({'running': 0, 'paused': 1}, 'paused'),
                 ({'instances': 2}, 'ambiguous')]
        for change, expected in cases:
            async with self.client(change) as client:
                data = (await client.get('/api/v1/metrics/containers')).json()
            row = data['containers'][0]
            self.assertEqual(row['state'], expected)
            self.assertTrue(all(v is None for v in row['metrics'].values()))

    async def test_nonfinite_missing_values_cpu_above_100_and_inconsistent_vectors(self):
        for value in ('NaN', '+Inf'):
            async with self.client({'cpu_usage_percent': value}) as client:
                response = await client.get('/api/v1/metrics/containers')
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.json()['containers'][0]['metrics']['cpu_usage_percent'])
        async with self.client({'cpu_usage_percent': 240}) as client:
            self.assertEqual((await client.get('/api/v1/metrics/containers')).json()['containers'][0]['metrics']['cpu_usage_percent'], 240)
        for change in ({'cpu_usage_percent': -1}, {'running': 2}, {'instances': 1.5},
                       {'instances': 0, 'present': 1}, {'running': 1, 'paused': 1}):
            async with self.client(change) as client:
                self.assertEqual((await client.get('/api/v1/metrics/containers')).status_code, 502)
        async with self.client(mode='duplicate') as client:
            self.assertEqual((await client.get('/api/v1/metrics/containers')).status_code, 502)

    async def test_disabled_and_offline_sources_are_explicit(self):
        for options in ({'enabled': False}, {'mode': 'offline'}):
            async with self.client(**options) as client:
                self.assertEqual((await client.get('/api/v1/metrics/containers')).status_code, 503)

    async def test_reader_and_local_worker_can_read_but_anonymous_and_actions_are_refused(self):
        user = {'id': 1, 'username': 'reader', 'display_name': 'Lecteur', 'role': 'reader', 'active': True}
        fixture = SimpleNamespace(check=lambda: None, authenticate=lambda _: {'user': user})
        with tempfile.TemporaryDirectory() as directory:
            token = 'c' * 64
            path = Path(directory) / 'worker.sha256'
            path.write_text(token_fingerprint(token)); path.chmod(0o600)
            async with self.client(auth_enabled=True, auth_store=fixture,
                                   auth_origins=['http://127.0.0.1:8088'], auth_secure_cookie=False,
                                   auth_worker_hash_file=str(path)) as client:
                self.assertEqual((await client.get('/api/v1/metrics/containers')).status_code, 401)
                self.assertEqual((await client.get('/api/v1/metrics/containers', headers={'Cookie': COOKIE + '=test'})).status_code, 200)
                headers = {'Authorization': 'Bearer ' + token}
                self.assertEqual((await client.get('/api/v1/metrics/containers', headers=headers)).status_code, 200)
                self.assertNotEqual((await client.post('/api/v1/metrics/containers', headers=headers)).status_code, 200)
        self.assertTrue(READ_PATH.fullmatch('/api/v1/metrics/containers'))
        self.assertFalse(READ_PATH.fullmatch('/api/v1/metrics/containers/restart'))

    def test_targets_cannot_inject_promql_or_duplicates(self):
        for target in ([{'project': 'x"}', 'service': 'php'}], [],
                       [{'project': 'x', 'service': 'php'}] * 2):
            with self.assertRaises(ValueError):
                parse_targets(target)
