"""Verifier Docker -> fichier atomique -> Node Exporter -> Prometheus -> vraie API."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

import httpx
import yaml

from prepare_api import prepare
from registry_sources import image_reference

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from container_monitoring.collector import DockerClient, atomic_write, collect


def docker(*arguments):
    result = subprocess.run(['docker', *arguments], check=True, text=True,
                            capture_output=True, timeout=45)
    return result.stdout.strip()


def free_port():
    with socket.socket() as handle:
        handle.bind(('127.0.0.1', 0))
        return handle.getsockname()[1]


async def read_api(context, url, targets):
    sys.path.insert(0, str(context))
    from app.main import create_app
    with patch.dict(os.environ, {'CONTAINER_METRICS_TARGETS': json.dumps(targets)}):
        app = create_app(prometheus_url=url, auth_enabled=False, container_metrics_enabled=True)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1') as client:
                response = await client.get('/api/v1/metrics/containers')
                response.raise_for_status()
                return response.json()


def main():
    project = 'ci-containers-' + uuid.uuid4().hex[:12]
    names = []
    moodle = yaml.safe_load((ROOT / 'ansible/roles/moodle/defaults/main.yml').read_text())
    monitoring = yaml.safe_load((ROOT / 'ansible/roles/monitoring/defaults/main.yml').read_text())
    workload = image_reference(moodle['moodle_postgres_image'])
    node_image = monitoring['monitoring_node_exporter_image'].replace('prom/node-exporter:', 'quay.io/prometheus/node-exporter:')
    targets = [{'project': project, 'service': service} for service in ('running', 'created', 'absent')]
    with tempfile.TemporaryDirectory(prefix='moodle-containers-ci-') as temporary:
        directory = Path(temporary)
        directory.chmod(0o755)
        textfile = directory / 'textfile'
        textfile.mkdir(mode=0o755)
        configuration = directory / 'prometheus.yml'
        node_port, prometheus_port = free_port(), free_port()
        try:
            for service in ('running', 'created'):
                name = project + '-' + service
                names.append(name)
                docker('create', '--name', name, '--label', 'com.docker.compose.project=' + project,
                       '--label', 'com.docker.compose.service=' + service, '--memory', '64m', '--cpus', '0.1',
                       '--entrypoint', '/usr/bin/sleep', workload, '300')
                if service == 'running':
                    docker('start', name)
            keys = [(item['project'], item['service']) for item in targets]
            content, report = collect(keys, DockerClient())
            assert report['status'] == 'available' and report['stats_collected'] == 1, report
            atomic_write(textfile / 'containers.prom', content)
            node = project + '-node'
            names.append(node)
            docker('run', '--detach', '--name', node, '--network', 'host',
                   '--volume', str(textfile) + ':/container-metrics:ro', node_image,
                   '--collector.disable-defaults', '--collector.textfile',
                   '--collector.textfile.directory=/container-metrics', '--web.listen-address=127.0.0.1:' + str(node_port))
            configuration.write_text(yaml.safe_dump({'global': {'scrape_interval': '1s'}, 'scrape_configs': [
                {'job_name': 'linux', 'static_configs': [{'targets': ['127.0.0.1:' + str(node_port)],
                   'labels': {'service': 'moodle-host', 'environment': 'pfe'}}]}]}))
            configuration.chmod(0o644)
            prometheus = project + '-prometheus'
            names.append(prometheus)
            docker('run', '--detach', '--name', prometheus, '--network', 'host',
                   '--volume', str(configuration) + ':/etc/prometheus/prometheus.yml:ro',
                   image_reference(monitoring['monitoring_prometheus_image']),
                   '--config.file=/etc/prometheus/prometheus.yml', '--storage.tsdb.path=/tmp/data',
                   '--web.listen-address=127.0.0.1:' + str(prometheus_port))
            context = directory / 'api'
            prepare(ROOT, context)
            url = 'http://127.0.0.1:' + str(prometheus_port)
            ready = False
            for _ in range(20):
                try:
                    ready = httpx.get(url + '/-/ready', timeout=2, trust_env=False).status_code == 200
                except httpx.HTTPError:
                    ready = False
                if ready:
                    break
                time.sleep(1)
            assert ready, 'Prometheus de test reste indisponible.'
            data = None
            for _ in range(20):
                data = asyncio.run(read_api(context, url, targets))
                if data['collector']['status'] == 'up':
                    break
                time.sleep(1)
            assert data is not None and data['collector']['status'] == 'up', data
            rows = {row['service']: row for row in data['containers']}
            assert rows['running']['state'] == 'running' and rows['running']['metrics_status'] == 'available', rows
            assert rows['running']['metrics']['memory_limit_bytes'] == 64 * 1024 * 1024, rows
            assert rows['created']['state'] == 'not_running' and rows['absent']['state'] == 'missing', rows
            # Une erreur Docker doit remplacer les anciennes mesures, meme si le noeud reste collecte.
            failure, _ = collect(keys, DockerClient(str(directory / 'missing.sock')))
            atomic_write(textfile / 'containers.prom', failure)
            for _ in range(20):
                data = asyncio.run(read_api(context, url, targets))
                if data['collector']['status'] == 'down':
                    break
                time.sleep(1)
            assert data['collector']['status'] == 'down' and data['collection']['status'] == 'up', data
            assert all(row['state'] == 'unknown' and all(v is None for v in row['metrics'].values())
                       for row in data['containers']), data
            print(json.dumps({'status': 'passed', 'scope': 'ephemeral_ci_containers',
                              'checks': ['real_docker_stats', 'textfile_mount', 'prometheus', 'api', 'inventory_failure']}))
        finally:
            for name in reversed(names):
                subprocess.run(['docker', 'rm', '--force', name], capture_output=True, timeout=30)


if __name__ == '__main__':
    main()
