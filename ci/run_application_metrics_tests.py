"""Verifier les vrais exporteurs sur des conteneurs ephemeres et des donnees fictives."""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
import uuid

import httpx
import jinja2
import yaml

from prepare_api import prepare
from run_php_exporter_deadline_test import verify_deadline
from registry_sources import docker_arguments

ROOT = Path(__file__).resolve().parents[1]


def run(*args, input=None, check=True):
    args = tuple(docker_arguments(args))
    try:
        return subprocess.run(args, input=input, text=True, capture_output=True, check=check, timeout=120)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        # capture_output garde la cause Docker hors de la trace Python : la
        # publier avant le nettoyage, tout en conservant l'echec du test.
        status = ('code ' + str(exc.returncode) if isinstance(exc, subprocess.CalledProcessError)
                  else 'delai de ' + str(exc.timeout) + 's depasse')
        print('Commande CI en echec (' + status + ') : ' + ' '.join(args[:2]),
              file=sys.stderr, flush=True)
        for stream, output in (('stdout', exc.stdout), ('stderr', exc.stderr)):
            if output:
                if isinstance(output, bytes):
                    output = output.decode('utf-8', errors='replace')
                print(stream + ':\n' + output[-8000:], file=sys.stderr, flush=True)
        raise


def wait_until(check, timeout=60):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (OSError, ValueError, AssertionError, httpx.HTTPError) as exc:
            last = type(exc).__name__
        time.sleep(0.5)
    raise RuntimeError('Verification non confirmee dans le delai : ' + str(last))


def environment():
    result = jinja2.Environment(undefined=jinja2.StrictUndefined)
    result.filters.update(bool=bool, to_json=json.dumps,
                          b64decode=lambda value: base64.b64decode(value).decode())
    return result


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def response_ready(url):
    with urlopen(url, timeout=3) as response:
        return response.status == 200


def published_port(name, port):
    mappings = json.loads(run('docker', 'inspect', '--format', '{{json .NetworkSettings.Ports}}', name).stdout)
    bindings = mappings.get(str(port) + '/tcp')
    assert bindings and len(bindings) == 1, 'Publication du port absente : ' + name
    assert bindings[0]['HostIp'] == '127.0.0.1', 'Le port doit rester sur la boucle locale.'
    return int(bindings[0]['HostPort'])


def main():
    prefix = 'moodle-appmetrics-ci-' + uuid.uuid4().hex[:10]
    network = prefix + '_private'
    observability = prefix + '_observability'
    containers = []
    try:
        with tempfile.TemporaryDirectory(prefix=prefix) as directory:
            temporary = Path(directory)
            defaults = yaml.safe_load((ROOT / 'ansible/roles/application_monitoring/defaults/main.yml').read_text())
            deadline_result = verify_deadline(defaults['application_monitoring_php_image'],
                                              defaults['application_monitoring_php_scrape_timeout_seconds'])
            print(json.dumps(deadline_result, indent=2), flush=True)
            moodle = yaml.safe_load((ROOT / 'ansible/roles/moodle/defaults/main.yml').read_text())
            prometheus = yaml.safe_load((ROOT / 'ansible/roles/monitoring/defaults/main.yml').read_text())
            password = 'b' * 64  # Identite de fixture, jamais celle du pilote.
            (temporary / 'secrets').mkdir()
            password_file = temporary / 'secrets/postgres_exporter_password'
            password_file.write_text(password)
            password_file.chmod(0o444)
            php_config = temporary / 'php-fpm.conf'
            php_config.write_text(environment().from_string((ROOT / 'ansible/roles/moodle/templates/php-fpm.conf.j2').read_text()).render(
                moodle_fpm_max_children=5, moodle_fpm_status_enabled=True,
                moodle_fpm_status_port=9001, moodle_fpm_status_path='/fpm-status'))
            php_config.chmod(0o444)
            run('docker', 'network', 'create', '--internal', network)

            def start(suffix, image, *arguments):
                name = prefix + '-' + suffix
                command = ['docker', 'run', '--detach', '--name', name, '--network', network,
                           '--network-alias', suffix]
                command += list(arguments) + [image]
                containers.append(name)
                run(*command)
                return name

            pg = start('postgres', moodle['moodle_postgres_image'], '-e', 'POSTGRES_DB=moodle',
                       '-e', 'POSTGRES_PASSWORD=ci-admin-test-only')
            wait_until(lambda: run('docker', 'exec', pg, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres', '-d', 'moodle', check=False).returncode == 0)

            def sql(value, *, monitoring=False, check=True):
                command = ['docker', 'exec', '-i']
                if monitoring:
                    command += ['-e', 'PGPASSWORD=' + password]
                command += [pg, 'psql', '-U', 'moodle_metrics' if monitoring else 'postgres',
                            '-d', 'moodle', '-v', 'ON_ERROR_STOP=1', '-A', '-t']
                if monitoring:
                    command += ['-h', '127.0.0.1']
                return run(*command, input=value, check=check)

            sql('CREATE TABLE public.fake_quiz_results (score integer); INSERT INTO public.fake_quiz_results VALUES (17);')
            template = environment().from_string((ROOT / 'ansible/roles/application_monitoring/templates/postgres-monitor.sql.j2').read_text())
            def provision(exists):
                return template.render(application_monitoring_database_user='moodle_metrics',
                    application_monitoring_password_file={'stat': {'exists': exists}},
                    application_monitoring_database_password={'content': base64.b64encode(password.encode()).decode()})
            first = sql(provision(False))
            assert 'MONITORING_CHANGED' in first.stderr
            assert 'MONITORING_CHANGED' not in sql(provision(True)).stderr, 'La seconde execution SQL doit rester sans changement.'
            check = sql("SELECT current_setting('default_transaction_read_only'), current_setting('statement_timeout'), pg_has_role(current_user,'pg_monitor','MEMBER');", monitoring=True)
            assert check.stdout.strip() == 'on|3s|t'
            assert sql('SELECT * FROM public.fake_quiz_results;', monitoring=True, check=False).returncode != 0
            assert sql('INSERT INTO public.fake_quiz_results VALUES (19);', monitoring=True, check=False).returncode != 0

            start('php', moodle['moodle_php_base_image'], '--mount', f'type=bind,src={php_config},dst=/usr/local/etc/php-fpm.d/zz-moodle.conf,readonly')
            values = dict(defaults)
            values.update(application_monitoring_base_dir=str(temporary),
                          application_monitoring_project_name=prefix,
                          application_monitoring_moodle_project=prefix,
                          application_monitoring_php_status_port=9001,
                          application_monitoring_php_status_path='/fpm-status',
                          application_monitoring_php_port='', application_monitoring_postgres_port='')
            compose_file = temporary / 'compose.yml'
            compose_file.write_text(environment().from_string(
                (ROOT / 'ansible/roles/application_monitoring/templates/compose.yml.j2').read_text()).render(values))
            php_name, pg_name = prefix + '-php-exporter-1', prefix + '-postgres-exporter-1'
            containers.extend([php_name, pg_name])
            run('docker', 'compose', '-p', prefix, '-f', str(compose_file),
                'up', '--detach', '--wait', '--wait-timeout', '60')
            php_port, pg_port = published_port(php_name, 9253), published_port(pg_name, 9187)
            for name, port in ((php_name, php_port), (pg_name, pg_port)):
                networks = json.loads(run('docker', 'inspect', '--format', '{{json .NetworkSettings.Networks}}', name).stdout)
                assert set(networks) == {network, observability}
                wait_until(lambda port=port: response_ready(f'http://127.0.0.1:{port}/metrics'))
            prom_config = temporary / 'prometheus.yml'
            prom_config.write_text(yaml.safe_dump({'global': {'scrape_interval': '2s'}, 'scrape_configs': [
                {'job_name': 'moodle_phpfpm', 'scrape_timeout': '2s', 'static_configs': [{'targets': [f'127.0.0.1:{php_port}']}]},
                {'job_name': 'moodle_postgres', 'scrape_timeout': '2s', 'static_configs': [{'targets': [f'127.0.0.1:{pg_port}']}]}]}))
            prom_config.chmod(0o444)
            prom_port = free_port()
            prom_name = prefix + '-prometheus'
            containers.append(prom_name)
            run('docker', 'run', '--detach', '--name', prom_name, '--network', 'host',
                '--mount', f'type=bind,src={prom_config},dst=/etc/prometheus/prometheus.yml,readonly',
                prometheus['monitoring_prometheus_image'], '--config.file=/etc/prometheus/prometheus.yml',
                '--storage.tsdb.path=/prometheus', f'--web.listen-address=127.0.0.1:{prom_port}')
            prom_url = f'http://127.0.0.1:{prom_port}'
            wait_until(lambda: response_ready(prom_url + '/-/ready'))
            context = temporary / 'api-context'
            prepare(ROOT, context)
            sys.path[:0] = [str(context), str(ROOT)]
            from app.main import create_app

            async def snapshot():
                application = create_app(prometheus_url=prom_url, auth_enabled=False, application_metrics_enabled=True)
                async with application.router.lifespan_context(application):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url='http://fixture') as client:
                        response = await client.get('/api/v1/metrics/applications')
                        assert response.status_code == 200, response.text
                        return response.json()
            def available():
                value = asyncio.run(snapshot())
                return value if value['status'] == 'available' else False
            observed = wait_until(available)
            assert observed['services']['php_fpm']['service_reachable'] is True
            assert observed['services']['postgresql']['service_reachable'] is True
            assert observed['services']['postgresql']['metrics']['max_connections'] > 0

            run('docker', 'stop', prefix + '-php')
            def service_failed():
                value = asyncio.run(snapshot())['services']['php_fpm']
                return value if value['service_reachable'] is False and value['collection']['status'] == 'up' else False
            stopped = wait_until(service_failed)
            assert all(value is None for value in stopped['metrics'].values())
            run('docker', 'stop', php_name)
            def exporter_failed():
                value = asyncio.run(snapshot())['services']['php_fpm']
                return value if value['collection']['status'] == 'down' else False
            assert wait_until(exporter_failed)['service_reachable'] is None
            print(json.dumps({'status': 'passed', 'scope': 'isolated_containers_with_fictitious_data',
                              'checks': ['actual_compose_loopback_ports', 'host_network_prometheus',
                                         'postgres_role_idempotence', 'no_pedagogical_table_access', 'read_only_role',
                                         'real_php_and_postgres_metrics', 'service_failure_distinct_from_exporter_failure'],
                              'production_changed': False}, indent=2))
    finally:
        for name in reversed(containers):
            run('docker', 'rm', '--force', '--volumes', name, check=False)
        run('docker', 'network', 'rm', observability, check=False)
        run('docker', 'network', 'rm', network, check=False)


if __name__ == '__main__':
    main()
