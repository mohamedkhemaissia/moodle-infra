"""Verifier les regles puis le trajet Prometheus -> Alertmanager -> webhook local isole."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
import uuid

import yaml

from alertmanager_config import render, values
from alertmanager_rule_cases import rule_cases
from registry_sources import image_reference

HTTP = build_opener(ProxyHandler({}))


def request(url, document=None):
    data = None if document is None else json.dumps(document).encode()
    with HTTP.open(Request(url, data=data, headers={'Content-Type': 'application/json'}), timeout=2) as response:
        body = response.read()
        return json.loads(body) if body and response.headers.get_content_type() == 'application/json' else body


def wait_for(predicate, message, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            result = predicate()
            if result:
                return result
        except (URLError, HTTPError, OSError):
            pass
        time.sleep(0.25)
    raise AssertionError(message)


def free_port():
    with socket.socket() as stream:
        stream.bind(('127.0.0.1', 0))
        return stream.getsockname()[1]


class Programs:
    def __init__(self, directory, bin_dir=None):
        self.directory, self.bin_dir = directory, bin_dir
        parameters = values()
        self.images = {name: image_reference(parameters[key]) for name, key in (
            ('prometheus', 'monitoring_prometheus_image'), ('promtool', 'monitoring_prometheus_image'),
            ('alertmanager', 'monitoring_alertmanager_image'), ('amtool', 'monitoring_alertmanager_image'))}
        self.running = {}
        self.volumes = set()
        self.prefix = 'ci-alertmanager-' + uuid.uuid4().hex[:12]

    def path(self, path):
        return str(path) if self.bin_dir else '/fixture/' + str(path.relative_to(self.directory))

    def storage_path(self, binary):
        if not self.bin_dir:
            return '/' + binary
        path = self.directory / (binary + '-data')
        path.mkdir(exist_ok=True)
        return str(path)

    def command(self, binary, *arguments, server=False, name=None):
        if self.bin_dir:
            return [str(self.bin_dir / binary), *arguments]
        command = ['docker', 'run', '--name', name or self.prefix + '-check-' + uuid.uuid4().hex[:8]]
        command += ['--detach'] if server else ['--rm']
        command += ['--network', 'host' if server else 'none', '--read-only', '--user', '65534:65534',
                    '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                    '--volume', str(self.directory) + ':/fixture:ro', '--tmpfs', '/tmp:rw,noexec,nosuid,size=8m',
                    '--memory', '192m', '--cpus', '0.5']
        if server:
            # Docker gere les donnees du compte 65534 et les reutilise au redemarrage.
            volume = self.prefix + '-' + binary + '-data'
            self.volumes.add(volume)
            command += ['--volume', volume + ':' + self.storage_path(binary)]
        command += ['--entrypoint', '/bin/' + binary, self.images[binary]]
        return command + list(arguments)

    def check(self, binary, *arguments):
        subprocess.run(self.command(binary, *arguments), check=True, timeout=90)

    def start(self, binary, *arguments):
        name = self.prefix + '-' + binary
        if self.bin_dir:
            log = (self.directory / (binary + '.log')).open('ab')
            process = subprocess.Popen(self.command(binary, *arguments), stdout=log, stderr=log)
            self.running[binary] = (process, log)
        else:
            subprocess.run(self.command(binary, *arguments, server=True, name=name), check=True,
                           capture_output=True, timeout=30)
            self.running[binary] = name

    def stop(self, binary):
        current = self.running.pop(binary, None)
        if current is None:
            return
        if self.bin_dir:
            process, log = current
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            log.close()
        else:
            # L arret gracieux ecrit notamment les silences avant de recreer Alertmanager.
            subprocess.run(['docker', 'stop', '--time', '10', current], check=True, capture_output=True, timeout=30)
            logs = subprocess.run(['docker', 'logs', current], capture_output=True, timeout=10)
            with (self.directory / (binary + '.log')).open('ab') as log:
                log.write(logs.stdout + logs.stderr)
            subprocess.run(['docker', 'rm', '--force', current], check=True, capture_output=True, timeout=30)

    def close(self):
        for binary in list(self.running):
            self.stop(binary)
        if self.volumes:
            subprocess.run(['docker', 'volume', 'rm', *sorted(self.volumes)], check=True,
                           capture_output=True, timeout=30)
            self.volumes.clear()


def write_yaml(path, document):
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    path.chmod(0o644)


def validate_rules(programs):
    directory = programs.directory
    document = render('alerts')
    names = [rule['alert'] for group in document['groups'] for rule in group['rules']]
    rules = directory / 'rules.yml'
    write_yaml(rules, document)
    tests = directory / 'rules-tests.yml'
    cases = rule_cases(names)
    write_yaml(tests, cases)
    programs.check('promtool', 'check', 'rules', programs.path(rules))
    programs.check('promtool', 'test', 'rules', programs.path(tests))
    for enabled in (False, True):
        config = directory / 'alertmanager-check.yml'
        parameters = values(monitoring_alert_email_enabled=enabled,
                            monitoring_alert_smtp_smarthost='smtp.example.test:587',
                            monitoring_alert_smtp_from='moodle@example.test',
                            monitoring_alert_email_to='operator@example.test',
                            monitoring_alert_smtp_username='moodle@example.test')
        alertmanager = render('alertmanager', parameters)
        if enabled:
            # Un secret fictif uniquement ; le secret du pilote n'est jamais requis par ces tests.
            password = directory / 'smtp-fixture'
            password.write_text('ci-fixture-only')
            password.chmod(0o644)
            alertmanager['global']['smtp_auth_password_file'] = programs.path(password)
        write_yaml(config, alertmanager)
        programs.check('amtool', 'check-config', programs.path(config))
    # Parser aussi le raccordement exact produit par le role, sans exiger les cibles du pilote.
    prometheus = render('prometheus')
    prometheus['rule_files'] = [programs.path(rules)]
    config = directory / 'prometheus-check.yml'
    write_yaml(config, prometheus)
    programs.check('promtool', 'check', 'config', programs.path(config))
    if not programs.bin_dir:
        compose = directory / 'compose.yml'
        write_yaml(compose, render('compose'))
        subprocess.run(['docker', 'compose', '-f', str(compose), 'config', '--quiet'], check=True, timeout=30)
    return len(cases['tests'])


def integration(programs):
    notifications, mutex = [], threading.Lock()
    state = {'probe_success': 1, 'collection': True}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            with mutex:
                healthy, collecting = state['probe_success'], state['collection']
            self.send_response(200 if collecting else 503)
            self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
            self.end_headers()
            self.wfile.write(f'probe_success {healthy}\n'.encode())

        def do_POST(self):
            document = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            with mutex:
                notifications.append(document)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    am_port, prom_port = free_port(), free_port()
    am_url = 'http://127.0.0.1:' + str(am_port)
    prom_url = 'http://127.0.0.1:' + str(prom_port)
    fixture_url = 'http://127.0.0.1:' + str(server.server_port)
    directory = programs.directory
    alertmanager = render('alertmanager')
    alertmanager['route'].update(group_wait='1s', group_interval='2s', repeat_interval='1h')
    alertmanager['route']['routes'][0].update(group_wait='1s', group_interval='2s', repeat_interval='1h')
    alertmanager['receivers'][0]['webhook_configs'] = [{'url': fixture_url + '/notifications', 'send_resolved': True}]
    am_config = directory / 'alertmanager.yml'
    write_yaml(am_config, alertmanager)
    am_args = ('--config.file=' + programs.path(am_config), '--storage.path=' + programs.storage_path('alertmanager'),
               '--web.listen-address=127.0.0.1:' + str(am_port), '--cluster.listen-address=')

    def received(name, status):
        with mutex:
            return [item for item in notifications if any(alert['labels']['alertname'] == name
                    and alert['status'] == status for alert in item['alerts'])]

    def active(name):
        return [item for item in request(am_url + '/api/v2/alerts') if item['labels']['alertname'] == name]

    def prometheus_active(name):
        return [item for item in request(prom_url + '/api/v1/alerts')['data']['alerts']
                if item['labels']['alertname'] == name]

    def post_alerts(items):
        now = datetime.now(timezone.utc)
        return request(am_url + '/api/v2/alerts', [{'labels': labels,
                       'startsAt': now.isoformat(), 'endsAt': (now + timedelta(minutes=5)).isoformat()}
                       for labels in items])

    try:
        programs.start('alertmanager', *am_args)
        wait_for(lambda: request(am_url + '/-/ready'), 'Alertmanager reste indisponible.')
        selected = [rule for group in render('alerts')['groups'] for rule in group['rules']
                    if rule['alert'] in ('MoodleHTTPSUnavailable', 'MoodleHTTPSCollectionUnavailable')]
        for rule in selected:
            rule['for'] = '1s'  # Accelerer uniquement les delais de cette fixture isolee.
        live_rules = directory / 'live-rules.yml'
        write_yaml(live_rules, {'groups': [{'name': 'ci-live', 'rules': selected}]})
        live_config = directory / 'prometheus.yml'
        write_yaml(live_config, {'global': {'scrape_interval': '1s', 'evaluation_interval': '1s'},
                   'rule_files': [programs.path(live_rules)],
                   'alerting': {'alertmanagers': [{'static_configs': [{'targets': ['127.0.0.1:' + str(am_port)]}]}]},
                   'scrape_configs': [{'job_name': 'moodle_https', 'static_configs': [
                       {'targets': ['127.0.0.1:' + str(server.server_port)]}]}]})
        programs.start('prometheus', '--config.file=' + programs.path(live_config),
                       '--storage.tsdb.path=' + programs.storage_path('prometheus'),
                       '--web.listen-address=127.0.0.1:' + str(prom_port))
        wait_for(lambda: request(prom_url + '/-/ready'), 'Prometheus reste indisponible.')
        with mutex:
            state['probe_success'] = 0
        wait_for(lambda: received('MoodleHTTPSUnavailable', 'firing'), 'Notification de panne HTTPS absente.')
        with mutex:
            state['probe_success'] = 1
        wait_for(lambda: received('MoodleHTTPSUnavailable', 'resolved'), 'Retablissement HTTPS non notifie.')
        previous = len(received('MoodleHTTPSUnavailable', 'firing'))
        with mutex:
            state['probe_success'] = 0
        wait_for(lambda: len(received('MoodleHTTPSUnavailable', 'firing')) > previous,
                 'Nouvelle panne HTTPS non notifiee apres retablissement.')
        with mutex:
            state['collection'] = False
        wait_for(lambda: received('MoodleHTTPSCollectionUnavailable', 'firing'), 'Perte de collecte non notifiee.')
        # La collecte perdue inhibe les notifications HTTPS, meme une resolution.
        # Verifier l etat de la regle sans confondre absence de mesure et retablissement.
        wait_for(lambda: not prometheus_active('MoodleHTTPSUnavailable'),
                 'Panne HTTPS encore declaree sans collecte disponible.')
        with mutex:
            state.update(collection=True, probe_success=1)
        wait_for(lambda: received('MoodleHTTPSCollectionUnavailable', 'resolved'), 'Reprise de collecte non notifiee.')

        grouped = [{'alertname': 'FixtureGroup', 'scope': 'fixture', 'compose_service': service,
                    'environment': 'pfe', 'category': 'container', 'source_job': 'fixture'}
                   for service in ('apache', 'php')]
        post_alerts(grouped)
        wait_for(lambda: received('FixtureGroup', 'firing'), 'Notification de groupe absente.')
        assert len(received('FixtureGroup', 'firing')[0]['alerts']) == 2, 'Alertes du meme groupe separees.'
        post_alerts(grouped)
        time.sleep(3)  # Plus d un group_interval, bien moins d un repeat_interval.
        assert len(received('FixtureGroup', 'firing')) == 1, 'Notification dupliquee.'

        context = {'scope': 'fixture', 'environment': 'pfe', 'source_job': 'linux'}
        post_alerts([{**context, 'alertname': 'FixtureCollection', 'category': 'collection'},
                     {**context, 'alertname': 'FixtureResource', 'category': 'resource'},
                     {**context, 'alertname': 'FixtureIndependent', 'category': 'service', 'source_job': 'moodle_postgres'}])
        wait_for(lambda: active('FixtureResource') and active('FixtureResource')[0]['status']['inhibitedBy'],
                 'Alerte dependante non inhibee.')
        assert active('FixtureIndependent')[0]['status']['state'] == 'active', 'Inhibition trop large.'

        now = datetime.now(timezone.utc)
        silence = request(am_url + '/api/v2/silences', {'matchers': [
            {'name': 'alertname', 'value': 'FixtureGroup', 'isRegex': False}], 'startsAt': now.isoformat(),
            'endsAt': (now + timedelta(minutes=5)).isoformat(), 'createdBy': 'ci', 'comment': 'Test isole'})
        identity = silence['silenceID']
        wait_for(lambda: active('FixtureGroup') and identity in active('FixtureGroup')[0]['status']['silencedBy'],
                 'Silence non applique.')
        programs.stop('alertmanager')
        programs.start('alertmanager', *am_args)
        wait_for(lambda: request(am_url + '/-/ready'), 'Alertmanager ne redemarre pas.')
        assert request(am_url + '/api/v2/silence/' + identity)['status']['state'] == 'active', 'Silence perdu au redemarrage.'
    finally:
        programs.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir', type=Path, help='Binaires upstream de meme version pour une execution sans Docker.')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='moodle-alertmanager-ci-') as temporary:
        directory = Path(temporary)
        directory.chmod(0o755)
        programs = Programs(directory, args.bin_dir)
        try:
            count = validate_rules(programs)
            integration(programs)
        except BaseException:
            for log in directory.glob('*.log'):
                print(log.name + '\n' + log.read_text()[-6000:])
            raise
        finally:
            programs.close()
    print(json.dumps({'status': 'passed', 'scope': 'isolated_alerting', 'rule_scenarios': count,
                      'checks': ['configurations', 'rule_lifecycle', 'prometheus_delivery', 'resolved_webhook',
                                 'grouping', 'deduplication', 'scoped_inhibition', 'persistent_silences']}))


if __name__ == '__main__':
    main()
