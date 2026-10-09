import base64
from pathlib import Path
import unittest

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[2]


class ConfigurationTests(unittest.TestCase):
    def render(self, relative, values):
        environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
        environment.filters.update(to_json=__import__('json').dumps, bool=bool,
                                   b64decode=lambda value: base64.b64decode(value).decode())
        return environment.from_string((ROOT / relative).read_text()).render(values)

    def defaults(self):
        values = yaml.safe_load((ROOT / 'ansible/roles/application_monitoring/defaults/main.yml').read_text())
        values.update(application_monitoring_php_status_port=9001, application_monitoring_php_status_path='/fpm-status')
        return values

    def test_exporters_have_only_loopback_ports_and_no_docker_socket_or_student_volumes(self):
        document = yaml.safe_load(self.render('ansible/roles/application_monitoring/templates/compose.yml.j2', self.defaults()))
        for service in document['services'].values():
            self.assertTrue(all(port.startswith('127.0.0.1:') for port in service['ports']))
            self.assertTrue(service['read_only'])
            self.assertEqual(service['cap_drop'], ['ALL'])
            self.assertNotIn('volumes', service)
            self.assertEqual(service['user'], '65534:65534')
        postgres = document['services']['postgres-exporter']
        self.assertEqual(postgres['environment']['DATA_SOURCE_PASS_FILE'], '/run/secrets/postgres_exporter_password')
        self.assertNotIn('DATA_SOURCE_PASS', postgres['environment'])
        self.assertNotIn('DATA_SOURCE_NAME', postgres['environment'])
        self.assertTrue(document['networks']['moodle_private']['external'])

    def test_published_metrics_use_a_non_internal_bridge_and_keep_moodle_private_network(self):
        document = yaml.safe_load(self.render('ansible/roles/application_monitoring/templates/compose.yml.j2', self.defaults()))
        bridge = document['networks']['observability']
        self.assertEqual(bridge['driver'], 'bridge')
        self.assertIs(bridge['internal'], False)
        for service in document['services'].values():
            self.assertEqual(set(service['networks']), {'moodle_private', 'observability'})
        moodle = (ROOT / 'ansible/roles/moodle/tasks/infrastructure.yml').read_text()
        self.assertIn('internal: true', moodle)

    def test_status_configuration_survives_full_moodle_redeployment_and_can_be_disabled(self):
        values = {'moodle_fpm_max_children': 5, 'moodle_fpm_status_enabled': True,
                  'moodle_fpm_status_port': 9001, 'moodle_fpm_status_path': '/fpm-status'}
        document = self.render('ansible/roles/moodle/templates/php-fpm.conf.j2', values)
        self.assertEqual(document.count('pm.status_path = /fpm-status'), 1)
        self.assertEqual(document.count('pm.status_listen = 9001'), 1)
        self.assertIn('; BEGIN PFE_APPLICATION_MONITORING', document)
        values['moodle_fpm_status_enabled'] = False
        self.assertNotIn('pm.status_listen', self.render('ansible/roles/moodle/templates/php-fpm.conf.j2', values))

    def test_php_deadline_is_shorter_than_prometheus_timeout_and_image_is_pinned(self):
        values = self.defaults()
        document = yaml.safe_load(self.render('ansible/roles/application_monitoring/templates/compose.yml.j2', values))
        php = document['services']['php-exporter']
        self.assertRegex(php['image'], r'^ghcr.io/hackthebox/php-fpm_exporter:2\.5\.0@sha256:[0-9a-f]{64}$')
        self.assertEqual(php['entrypoint'], ['/php-fpm_exporter'])
        self.assertEqual(php['command'][0], 'server')
        self.assertIn('--phpfpm.scrape-timeout=3s', php['command'])
        monitoring = yaml.safe_load((ROOT / 'ansible/roles/monitoring/defaults/main.yml').read_text())
        monitoring.update(monitoring_application_metrics_enabled=True,
                          monitoring_application_php_port=9253, monitoring_application_postgres_port=9187,
                          monitoring_moodle_probe_url='https://moodle-pfe.test:8443/login/index.php')
        prom = yaml.safe_load(self.render('ansible/roles/monitoring/templates/prometheus.yml.j2', monitoring))
        timeout = next(job for job in prom['scrape_configs'] if job['job_name'] == 'moodle_phpfpm')['scrape_timeout']
        self.assertLess(values['application_monitoring_php_scrape_timeout_seconds'], int(timeout[:-1]))

    def test_targeted_fix_cannot_restart_php_or_change_database_or_other_services(self):
        tasks = yaml.safe_load((ROOT / 'ansible/roles/application_monitoring/tasks/main.yml').read_text())
        selected = [task for task in tasks if 'php-exporter-fix' in task.get('tags', [])]
        commands = [task['ansible.builtin.command']['argv'] for task in selected if 'ansible.builtin.command' in task]
        self.assertFalse(any('restart' in command or 'exec' in command for command in commands))
        compose_tasks = [task['community.docker.docker_compose_v2'] for task in selected if 'community.docker.docker_compose_v2' in task]
        self.assertEqual(len(compose_tasks), 1)
        self.assertEqual(compose_tasks[0]['services'], ['php-exporter'])
        self.assertIs(compose_tasks[0]['dependencies'], False)

    def test_monitoring_job_extension_does_not_replace_linux_or_https(self):
        values = yaml.safe_load((ROOT / 'ansible/roles/monitoring/defaults/main.yml').read_text())
        values.update(monitoring_application_metrics_enabled=True, monitoring_application_php_port=9253,
                      monitoring_application_postgres_port=9187, monitoring_moodle_probe_url='https://moodle-pfe.test:8443/login/index.php')
        document = yaml.safe_load(self.render('ansible/roles/monitoring/templates/prometheus.yml.j2', values))
        self.assertEqual({job['job_name'] for job in document['scrape_configs']},
                         {'prometheus', 'linux', 'blackbox_exporter', 'moodle_https', 'moodle_phpfpm', 'moodle_postgres'})
        php = next(job for job in document['scrape_configs'] if job['job_name'] == 'moodle_phpfpm')
        self.assertEqual(php['metric_relabel_configs'][0]['action'], 'drop')

    def test_sql_renders_a_separate_limited_role_without_table_permissions(self):
        values = self.defaults()
        values.update(application_monitoring_password_file={'stat': {'exists': True}},
                      application_monitoring_database_password={'content': base64.b64encode(b'b' * 64).decode()})
        sql = self.render('ansible/roles/application_monitoring/templates/postgres-monitor.sql.j2', values)
        self.assertIn('CREATE ROLE moodle_metrics LOGIN', sql)
        self.assertIn('NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS', sql)
        self.assertIn('GRANT pg_monitor TO moodle_metrics', sql)
        self.assertIn('default_transaction_read_only = on', sql)
        self.assertNotIn('GRANT SELECT', sql)
        self.assertNotIn('ALTER USER moodle ', sql)
        self.assertNotIn('{{', sql)
