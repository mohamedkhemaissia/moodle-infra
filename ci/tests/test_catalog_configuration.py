import json
from pathlib import Path
import shlex
import unittest

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[2]


class CatalogConfigurationTests(unittest.TestCase):
    def render(self, path, values):
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        env.filters.update(to_json=json.dumps, bool=bool,
                           ternary=lambda condition, yes, no: yes if condition else no)
        return env.from_string((ROOT / path).read_text()).render(values)

    def test_collector_service_has_bounded_private_execution_and_no_repository_write_paths(self):
        values = dict(backup_catalog_base_dir='/opt/moodle-backup-catalog',
                      backup_catalog_output_dir='/var/lib/moodle-backup-status',
                      backup_catalog_project='moodle-pilote', backup_catalog_marker='/opt/moodle/backups/last-success.txt',
                      backup_catalog_moodle_staging='/opt/moodle/backups/staging',
                      backup_catalog_pilotage_root='/var/lib/moodle-pilotage-backup',
                      backup_catalog_pilotage_enabled=True)
        text = self.render('ansible/roles/backup_catalog/templates/moodle-backup-catalog.service.j2', values)
        settings = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
        argv = shlex.split(settings['ExecStart'])
        self.assertEqual(argv[:4], ['/usr/bin/python3', '-B', '-m', 'backup_status.catalog_collector'])
        self.assertEqual(argv[argv.index('--pilotage-root')+1], values['backup_catalog_pilotage_root'])
        self.assertEqual(settings['ReadWritePaths'], values['backup_catalog_output_dir'])
        self.assertEqual(settings['ProtectSystem'], 'strict')
        self.assertEqual(settings['KillMode'], 'control-group')
        self.assertEqual(settings['CapabilityBoundingSet'], '')
        self.assertGreater(int(settings['TimeoutStartSec']), 45)
        self.assertNotIn('docker.sock', text)
        values['backup_catalog_pilotage_enabled'] = False
        self.assertNotIn('--pilotage-root', self.render(
            'ansible/roles/backup_catalog/templates/moodle-backup-catalog.service.j2', values))

    def test_api_mounts_only_public_catalogue_and_does_not_acquire_restic_credentials(self):
        values = yaml.safe_load((ROOT / 'ansible/roles/control_plane/defaults/main.yml').read_text())
        values.update(control_plane_image='fixture/api:latest', control_plane_application_php_pool='www',
                      control_plane_application_pg_database='moodle',
                      control_plane_backup_status_enabled=True, control_plane_backup_catalog_enabled=True,
                      pilotage_auth_activation={'stat': {'exists': False}},
                      pilotage_policy_activation={'stat': {'exists': False}},
                      pilotage_database_activation={'stat': {'exists': False}})
        doc = yaml.safe_load(self.render('ansible/roles/control_plane/templates/compose.yml.j2', values))
        api = doc['services']['api']
        self.assertEqual(api['environment']['BACKUP_CATALOG_FILE'], '/var/lib/moodle-backup-status/catalog.json')
        self.assertEqual(api['volumes'], [dict(type='bind', source='/var/lib/moodle-backup-status',
                                              target='/var/lib/moodle-backup-status', read_only=True)])
        self.assertNotIn('restic', json.dumps(doc).lower())
        values.update(control_plane_backup_status_enabled=False, control_plane_backup_catalog_enabled=False)
        disabled = yaml.safe_load(self.render('ansible/roles/control_plane/templates/compose.yml.j2', values))['services']['api']
        self.assertNotIn('BACKUP_CATALOG_FILE', disabled['environment'])
        self.assertNotIn('volumes', disabled)

    def test_increment_never_redeploys_moodle_and_installs_canonical_shared_source(self):
        from prepare_api import SHARED
        playbook = yaml.safe_load((ROOT / 'ansible/playbooks/backup-catalog.yml').read_text())
        self.assertEqual(playbook[0]['roles'], ['backup_status', 'backup_catalog', 'control_plane', 'dashboard'])
        self.assertEqual(SHARED['backup_status/catalog.py'], 'app/backup_catalog_status.py')
        tasks = yaml.safe_load((ROOT / 'ansible/roles/control_plane/tasks/main.yml').read_text())
        source = next(t['ansible.builtin.copy'] for t in tasks if t.get('ansible.builtin.copy', {}).get('dest', '').endswith('/app/backup_catalog_status.py'))
        self.assertEqual(source['src'], '{{ playbook_dir }}/../../backup_status/catalog.py')
        installer = next(t for t in tasks if t.get('name') == 'Copier les sources necessaires a l image')
        self.assertIn("'app/backup_catalog_status.py'", installer['loop'])
        # Les tests avec un processus chronometre s'executent en CI, pas pendant le deploiement de la VM.
        role = (ROOT / 'ansible/roles/backup_catalog/tasks/main.yml').read_text()
        self.assertNotIn('test_catalog_process.py', role)

    def test_pipeline_runs_real_restic_before_building_images_and_keeps_existing_jobs(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
        job = workflow['jobs']['backup-catalog']
        self.assertIn('python -B ci/run_backup_catalog_restic_test.py', [s.get('run') for s in job['steps']])
        self.assertEqual(job['strategy']['matrix']['include'], [
            {'python-version': '3.10.12', 'os': 'ubuntu-22.04'},
            {'python-version': '3.12.15', 'os': 'ubuntu-24.04'}])
        self.assertIn('python -B -m unittest discover -s backup_status/tests -q', [s.get('run') for s in job['steps']])
        self.assertIn('backup-catalog', workflow['jobs']['images']['needs'])
        self.assertIn('application-metrics', workflow['jobs']['images']['needs'])
        self.assertEqual(workflow['permissions'], {'contents': 'read'})


if __name__ == '__main__':
    unittest.main()
