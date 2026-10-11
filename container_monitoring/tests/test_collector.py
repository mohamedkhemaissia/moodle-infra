from datetime import datetime, timezone
import http.server
import json
from pathlib import Path
import socketserver
import tempfile
import threading
import unittest

from container_monitoring.collector import DockerClient, atomic_write, collect, stats_reading, targets_from_config

NOW = datetime(2026, 10, 10, 20, tzinfo=timezone.utc).timestamp()
IDENTITY = 'a' * 64
TARGET = ('moodle-pilote', 'php')


def container(project='moodle-pilote', service='php', state='running', identity=IDENTITY):
    return {'Id': identity, 'State': state, 'Status': 'Up one minute (healthy)',
            'Labels': {'com.docker.compose.project': project, 'com.docker.compose.service': service}}


def statistics():
    return {'id': IDENTITY, 'read': datetime.fromtimestamp(NOW, timezone.utc).isoformat(),
            'memory_stats': {'usage': 100, 'limit': 300, 'stats': {'inactive_file': 20}},
            'cpu_stats': {'cpu_usage': {'total_usage': 500}, 'system_cpu_usage': 1000, 'online_cpus': 3},
            'precpu_stats': {'cpu_usage': {'total_usage': 100}, 'system_cpu_usage': 500},
            'pids_stats': {'current': 2}}


class FixtureClient:
    def __init__(self, rows=None, stats=None, offline=False):
        self.rows = rows if rows is not None else [container()]
        self.values = statistics() if stats is None else stats
        self.offline, self.seen = offline, []

    def inventory(self):
        if self.offline:
            raise OSError('fixture')
        return self.rows

    def stats(self, identity):
        self.seen.append(identity)
        return self.values


class CollectorTests(unittest.TestCase):
    def test_exact_cpu_and_working_set_match_docker_semantics(self):
        reading = stats_reading(statistics(), IDENTITY, NOW)
        self.assertAlmostEqual(reading['cpu_usage_percent'], 240)
        self.assertEqual(reading['memory_working_set_bytes'], 80)
        value = statistics()
        value['memory_stats']['stats'] = {'total_inactive_file': 40, 'inactive_file': 20}
        self.assertEqual(stats_reading(value, IDENTITY, NOW)['memory_working_set_bytes'], 60)

    def test_first_cpu_sample_is_unknown_while_valid_zero_memory_survives(self):
        value = statistics()
        value['precpu_stats'] = {}
        value['memory_stats']['usage'] = 0
        value['memory_stats']['stats'] = {}
        reading = stats_reading(value, IDENTITY, NOW)
        self.assertNotIn('cpu_usage_percent', reading)
        self.assertEqual(reading['memory_working_set_bytes'], 0)

    def test_docker_nanosecond_dates_are_normalized_for_the_vm_python_version(self):
        value = statistics()
        value['read'] = '2026-10-10T20:00:00.123456789Z'
        self.assertAlmostEqual(stats_reading(value, IDENTITY, NOW + 1)['stats_timestamp_seconds'], NOW + 0.123456)

    def test_future_old_wrong_identity_and_unsafe_values_cannot_be_exported(self):
        for mutate in (lambda x: x.update(id='b' * 64),
                       lambda x: x.update(read=datetime.fromtimestamp(NOW - 91, timezone.utc).isoformat()),
                       lambda x: x.update(read=datetime.fromtimestamp(NOW + 6, timezone.utc).isoformat()),
                       lambda x: x['memory_stats'].update(usage=-1),
                       lambda x: x['memory_stats'].update(usage=float('inf'))):
            value = statistics(); mutate(value)
            with self.assertRaises(ValueError):
                stats_reading(value, IDENTITY, NOW)

    def test_restorations_and_oneoff_containers_are_excluded_by_compose_identity(self):
        oneoff = container(identity='b' * 64)
        oneoff['Labels']['com.docker.compose.oneoff'] = 'True'
        client = FixtureClient([container(), container('moodle-restauration'), oneoff])
        text, report = collect([TARGET], client, clock=lambda: NOW)
        self.assertEqual(client.seen, [IDENTITY])
        self.assertEqual(report['stats_collected'], 1)
        self.assertNotIn('restauration', text)

    def test_missing_stopped_and_scaled_services_are_not_reported_as_running(self):
        for rows, instances in (([], 0), ([container(state='exited')], 1),
                                ([container(), container(identity='b' * 64)], 2)):
            client = FixtureClient(rows)
            text, _ = collect([TARGET], client, clock=lambda: NOW)
            self.assertEqual(client.seen, [])
            self.assertIn('compose_service="php"} ' + str(instances), text)
            self.assertNotIn('cpu_usage_percent', text)
            if instances != 1:
                self.assertNotIn('moodle_container_running', text)
            else:
                self.assertIn('moodle_container_running{project="moodle-pilote",compose_service="php"} 0', text)

    def test_failed_inventory_replaces_old_measurements_with_failure_without_zero_filling(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'containers.prom'
            atomic_write(output, 'moodle_container_cpu_usage_percent 50\n')
            text, report = collect([TARGET], FixtureClient(offline=True), clock=lambda: NOW)
            atomic_write(output, text)
            self.assertEqual(report['status'], 'unavailable')
            self.assertIn('moodle_container_collection_success 0', output.read_text())
            self.assertNotIn('cpu_usage_percent', output.read_text())
            self.assertEqual(output.stat().st_mode & 0o777, 0o644)
            self.assertEqual(list(Path(directory).glob('.containers-*')), [])

    def test_failed_stats_preserve_inventory_but_do_not_create_zero_resources(self):
        text, report = collect([TARGET], FixtureClient(stats={}), clock=lambda: NOW)
        self.assertEqual(report['status'], 'partial')
        self.assertIn('moodle_container_running{project="moodle-pilote",compose_service="php"} 1', text)
        self.assertIn('moodle_container_stats_success{project="moodle-pilote",compose_service="php"} 0', text)
        self.assertNotIn('memory_working_set_bytes', text)

    def test_untrusted_targets_duplicates_and_unsafe_output_are_rejected(self):
        target = {'project': TARGET[0], 'service': TARGET[1]}
        for value in ({'targets': []}, {'targets': [target, target]},
                      {'targets': [{'project': 'bad"}', 'service': 'php'}]},
                      {'targets': [dict(target, password='hidden')]}):
            with self.assertRaises(ValueError):
                targets_from_config(value)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'original.prom'
            source.write_text('preserved')
            link = Path(directory) / 'containers.prom'
            link.symlink_to(source)
            with self.assertRaises(ValueError):
                atomic_write(link, 'replacement')
            self.assertEqual(source.read_text(), 'preserved')

    def test_real_unix_http_transport_is_bounded_and_can_only_issue_reads(self):
        seen = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.command, self.path))
                body = {'ApiVersion': '1.54'} if self.path == '/version' else (
                    [container()] if '/containers/json?' in self.path else statistics())
                encoded = json.dumps(body).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(encoded)))
                self.end_headers(); self.wfile.write(encoded)
            def log_message(self, *args):
                pass
        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'docker.sock')
            try:
                server = Server(path, Handler)
            except PermissionError:
                self.skipTest('La creation de sockets Unix est interdite dans cet environnement ; ce test reste actif en CI.')
            with server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    client = DockerClient(path)
                    _, report = collect([TARGET], client, clock=lambda: NOW)
                    self.assertEqual(report['stats_collected'], 1)
                    with self.assertRaises(ValueError):
                        client.get('/v1.54/containers/' + IDENTITY + '/stop')
                finally:
                    server.shutdown(); thread.join(timeout=2)
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(method == 'GET' for method, _ in seen))


if __name__ == '__main__':
    unittest.main()
