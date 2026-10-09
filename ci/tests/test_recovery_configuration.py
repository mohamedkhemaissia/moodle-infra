import json
from pathlib import Path
import unittest
import jinja2
import yaml
from prepare_api import SHARED

ROOT=Path(__file__).resolve().parents[2]


class RecoveryConfigurationTests(unittest.TestCase):
    def test_deployment_orders_backup_compatibility_before_new_journal_and_has_no_restore_command(self):
        value=yaml.safe_load((ROOT/'ansible/playbooks/recovery-requests.yml').read_text())[0]
        self.assertEqual(value['roles'],['pilotage_backup','recovery_requests','backup_status','backup_catalog','control_plane','dashboard'])
        role=(ROOT/'ansible/roles/recovery_requests/tasks/main.yml').read_text()
        self.assertNotIn('DROP',role); self.assertNotIn('restore-test',role); self.assertNotIn('test-restore',role)
        self.assertIn('pilotage.recovery_cli',role); self.assertIn('from_json).changed',role)

    def test_api_uses_shared_journal_and_requires_explicit_activation_without_docker_or_restic_access(self):
        self.assertEqual(SHARED['pilotage/recovery.py'],'pilotage/recovery.py')
        values=yaml.safe_load((ROOT/'ansible/roles/control_plane/defaults/main.yml').read_text())
        values.update(control_plane_image='api:fixture',control_plane_application_pg_database='moodle',
            control_plane_recovery_requests_enabled=True,control_plane_backup_status_enabled=True,control_plane_backup_catalog_enabled=True,
            pilotage_database_activation={'stat':{'exists':True}},pilotage_auth_activation={'stat':{'exists':True}})
        env=jinja2.Environment(undefined=jinja2.StrictUndefined)
        env.filters.update(to_json=json.dumps,bool=bool,ternary=lambda c,y,n:y if c else n)
        config=yaml.safe_load(env.from_string((ROOT/'ansible/roles/control_plane/templates/compose.yml.j2').read_text()).render(values))
        api=config['services']['api']; self.assertEqual(api['environment']['RECOVERY_REQUESTS_ENABLED'],'true')
        self.assertIn('BACKUP_CATALOG_FILE',api['environment'])
        self.assertTrue(all(m['read_only'] for m in api['volumes']))
        self.assertNotIn('docker.sock',json.dumps(config)); self.assertNotIn('restic',json.dumps(config))
        defaults=yaml.safe_load((ROOT/'ansible/roles/control_plane/defaults/main.yml').read_text())
        self.assertFalse(defaults['control_plane_recovery_requests_enabled'])
        self.assertIn('tests/test_recovery_requests_api.py',defaults['control_plane_source_files'])

    def test_dashboard_sources_and_pg_tests_are_included_in_existing_required_ci_jobs(self):
        files=yaml.safe_load((ROOT/'ansible/roles/dashboard/defaults/main.yml').read_text())['dashboard_source_files']
        for name in ('src/RecoveryRequests.jsx','src/recoveryRequests.js','tests/recoveryRequests.test.js'):
            self.assertIn(name,files)
        workflow=yaml.safe_load((ROOT/'.github/workflows/ci.yml').read_text())
        self.assertIn('postgresql',workflow['jobs']['images']['needs'])
        runner=(ROOT/'ci/run_postgresql_tests.py').read_text()
        self.assertIn('test_*.py',runner)
        self.assertTrue((ROOT/'tests/postgresql/test_recovery_requests.py').is_file())


if __name__=='__main__':unittest.main()
