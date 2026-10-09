"""Regression du delai FastCGI ; donnees fictives et exporteur Docker isole."""
from __future__ import annotations

from contextlib import contextmanager
import json
import re
import socket
import socketserver
import struct
import subprocess
import threading
import time
from urllib.request import urlopen
import uuid


def record(kind, request_id, content=b''):
    padding = (-len(content)) % 8
    return struct.pack('!BBHHBB', 1, kind, request_id, len(content), padding, 0) + content + b'\0' * padding


def read_exact(connection, length):
    chunks = []
    remaining = length
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise EOFError('Connexion FastCGI fermee')
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def status_document():
    return {
        'pool': 'www', 'process manager': 'dynamic', 'start time': 1700000000,
        'start since': 3600, 'accepted conn': 5, 'listen queue': 0,
        'max listen queue': 0, 'listen queue len': 128,
        'idle processes': 1, 'active processes': 1, 'total processes': 2,
        'max active processes': 2, 'max children reached': 0, 'slow requests': 0,
        'processes': [
            {'pid': pid, 'state': state, 'start time': 1700000000,
             'start since': 3600, 'requests': 3, 'request duration': 0,
             'request method': 'GET', 'request URI': '/fpm-status',
             'content length': 0, 'user': '-', 'script': '-',
             'last request cpu': 0, 'last request memory': 0}
            for pid, state in ((101, 'Idle'), (102, 'Running'))
        ],
    }


class StatusHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            request_id = 1
            # Attendre la fin du corps de la requete FastCGI, avec taille bornee.
            for _ in range(128):
                header = read_exact(self.request, 8)
                version, kind, request_id, length, padding, _ = struct.unpack('!BBHHBB', header)
                if version != 1:
                    return
                read_exact(self.request, length + padding)
                if kind == 5 and length == 0:
                    break
            else:
                return
            mode = self.server.mode
            if mode in ('stall', 'partial'):
                if mode == 'partial':
                    # Debut d'un en-tete valide ; bloquer aussi les lectures partielles.
                    self.request.sendall(record(6, request_id, b'Content-type: application/json\r\n\r\n')[:4])
                while not self.server.stopping.is_set():
                    try:
                        if not self.request.recv(1024):
                            return
                    except socket.timeout:
                        pass
                return
            body = b'Content-type: application/json\r\n\r\n' + json.dumps(status_document()).encode()
            self.request.sendall(record(6, request_id, body) + record(6, request_id)
                                 + record(3, request_id, b'\0' * 8))
        except (OSError, EOFError, ValueError):
            return


class StatusServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self):
        self.mode = 'healthy'
        self.stopping = threading.Event()
        super().__init__(('127.0.0.1', 0), StatusHandler)


@contextmanager
def fake_status_server():
    server = StatusServer()
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.stopping.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def numeric_metric(text, name):
    matches = re.findall(r'^' + re.escape(name) + r'(?:\{[^\n]*\})? ([0-9.eE+-]+)$', text, re.MULTILINE)
    if len(matches) != 1:
        raise AssertionError('Une seule serie attendue : ' + name)
    return float(matches[0])


def verify_deadline(image, timeout_seconds=3):
    name = 'moodle-fcgi-deadline-ci-' + uuid.uuid4().hex[:12]
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        web_port = listener.getsockname()[1]
    command = ['docker', 'run', '--detach', '--name', name, '--network', 'host',
               '--read-only', '--user', '65534:65534', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges:true', '--entrypoint', '/php-fpm_exporter', image]
    with fake_status_server() as fixture:
        command += ['server', f'--web.listen-address=127.0.0.1:{web_port}',
                    f'--phpfpm.scrape-uri=tcp://127.0.0.1:{fixture.server_address[1]}/fpm-status',
                    f'--phpfpm.scrape-timeout={timeout_seconds}s', '--log.level=warn']
        results = []
        try:
            subprocess.run(command, capture_output=True, text=True, check=True, timeout=120)
            base_url = f'http://127.0.0.1:{web_port}'
            deadline = time.monotonic() + 30
            while True:
                try:
                    with urlopen(base_url + '/', timeout=2) as response:
                        assert response.status == 200
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.2)

            def scrape(expected_up):
                started = time.monotonic()
                with urlopen(base_url + '/metrics', timeout=timeout_seconds + 4) as response:
                    assert response.status == 200
                    text = response.read(2_000_000).decode()
                elapsed = time.monotonic() - started
                assert elapsed < timeout_seconds + 3, 'La collecte reste bloquee.'
                assert numeric_metric(text, 'phpfpm_up') == expected_up, text
                results.append({'mode': fixture.mode, 'seconds': round(elapsed, 3), 'phpfpm_up': expected_up})
                return text

            scrape(1)
            initial_goroutines = numeric_metric(scrape(1), 'go_goroutines')
            for mode in ('stall', 'partial'):
                fixture.mode = mode
                for _ in range(3):
                    scrape(0)
                # Reprise sans recreer ou redemarrer le conteneur.
                fixture.mode = 'healthy'
                healthy = scrape(1)
                assert numeric_metric(healthy, 'phpfpm_total_processes') == 2
                assert numeric_metric(healthy, 'phpfpm_active_processes') == 1
                assert numeric_metric(healthy, 'go_goroutines') <= initial_goroutines + 8
            return {'status': 'passed', 'scope': 'isolated_exporter_with_fake_fastcgi',
                    'production_changed': False, 'scrapes': results,
                    'checks': ['read_deadline', 'partial_read_deadline', 'healthy_recovery_without_restart',
                               'metric_names_and_pool_counts', 'bounded_goroutines']}
        finally:
            subprocess.run(['docker', 'rm', '--force', name], capture_output=True, timeout=30, check=False)


if __name__ == '__main__':
    from pathlib import Path
    import yaml
    defaults = yaml.safe_load((Path(__file__).resolve().parents[1] /
                               'ansible/roles/application_monitoring/defaults/main.yml').read_text())
    print(json.dumps(verify_deadline(defaults['application_monitoring_php_image'],
                                    defaults['application_monitoring_php_scrape_timeout_seconds']), indent=2))
