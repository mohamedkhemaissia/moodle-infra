from pathlib import Path
import unittest

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[2]


class ContainerConfigurationTests(unittest.TestCase):
    def test_dashboard_verifies_container_metrics_without_loading_control_plane_role(self):
        defaults = yaml.safe_load((ROOT / 'ansible/roles/dashboard/defaults/main.yml').read_text())
        tasks = yaml.safe_load((ROOT / 'ansible/roles/dashboard/tasks/main.yml').read_text())
        task = next(task for task in tasks if task.get('register') == 'dashboard_containers_response')
        environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
        verify = environment.compile_expression(task['until'])
        expected = [{'project': 'moodle-pilote', 'service': service} for service in ('apache', 'php', 'postgres')]
        for variables, targets in (({}, expected), ({'container_monitoring_targets': expected[:1]}, expected[:1]),
                                  ({'container_monitoring_targets': expected, 'control_plane_container_metrics_targets': expected[:2]}, expected[:2])):
            with self.subTest(variables=variables):
                configured = yaml.safe_load(environment.from_string(defaults['dashboard_container_metrics_targets']).render(variables))
                self.assertEqual(configured, targets)
                rows = [{'state': 'running'} for _ in targets]
                context = {'dashboard_container_metrics_targets': configured,
                           'dashboard_containers_response': {'status': 200, 'json': {
                               'mode': 'read_only', 'collector': {'status': 'up'}, 'containers': rows}}}
                self.assertTrue(verify(**context))
                rows.pop()
                self.assertFalse(verify(**context))

    def test_textfile_uses_a_readonly_mount_and_does_not_add_a_docker_socket_to_exporters(self):
        values = yaml.safe_load((ROOT / 'ansible/roles/monitoring/defaults/main.yml').read_text())
        values.update(monitoring_container_metrics_enabled=True,
                      monitoring_container_textfile_dir='/var/lib/moodle-container-monitoring/textfile')
        environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
        environment.filters['bool'] = bool
        source = (ROOT / 'ansible/roles/monitoring/templates/compose.yml.j2').read_text()
        document = yaml.safe_load(environment.from_string(source).render(values))
        node = document['services']['node-exporter']
        self.assertIn('--collector.textfile.directory=/container-metrics', node['command'])
        self.assertIn('/var/lib/moodle-container-monitoring/textfile:/container-metrics:ro', node['volumes'])
        self.assertNotIn('docker.sock', str(document))
        self.assertEqual(set(document['services']), {'prometheus', 'node-exporter', 'blackbox'})
        values['monitoring_container_metrics_enabled'] = False
        self.assertNotIn('container-metrics', environment.from_string(source).render(values))

    def test_deployment_sources_include_the_new_api_ui_and_tests_without_missing_files(self):
        from prepare_api import SHARED
        for role, component, variable, shared in (
            ('control_plane', 'control-plane', 'control_plane_source_files', set(SHARED.values())),
            ('dashboard', 'dashboard', 'dashboard_source_files', set())):
            values = yaml.safe_load((ROOT / f'ansible/roles/{role}/defaults/main.yml').read_text())
            for path in values[variable]:
                if path not in shared:
                    self.assertTrue((ROOT / component / path).is_file(), f'Missing deployment source: {path}')
            self.assertEqual(len(values[variable]), len(set(values[variable])))

    def test_inventory_scopes_match_the_pilot_and_omit_restore_projects(self):
        from container_monitoring.collector import targets_from_config
        values = yaml.safe_load((ROOT / 'ansible/inventories/group_vars/moodle/container-monitoring.yml').read_text())
        targets = targets_from_config({'targets': values['container_monitoring_targets']})
        self.assertEqual(len(targets), 3)
        self.assertEqual({p for p, _ in targets}, {'moodle-pilote'})
        self.assertTrue(all('restauration' not in p and 'test-' not in p for p, _ in targets))
        self.assertEqual({s for p, s in targets if p == 'moodle-pilote'}, {'apache', 'php', 'postgres'})

    def test_playbook_does_not_redeploy_moodle_and_collector_service_is_local_and_bounded(self):
        play = yaml.safe_load((ROOT / 'ansible/playbooks/container-monitoring.yml').read_text())[0]
        self.assertEqual(play['roles'], ['container_monitoring', 'monitoring', 'control_plane', 'dashboard'])
        service = (ROOT / 'ansible/roles/container_monitoring/templates/moodle-container-metrics.service.j2').read_text()
        self.assertIn('RestrictAddressFamilies=AF_UNIX', service)
        self.assertIn('TimeoutStartSec=35', service)
        self.assertIn('MemoryMax=96M', service)
        self.assertIn('NoNewPrivileges=true', service)
