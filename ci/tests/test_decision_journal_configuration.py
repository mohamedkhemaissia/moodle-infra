from pathlib import Path
import json
import unittest

import jinja2
import yaml
from pilotage.worker_auth import READ_PATH

ROOT = Path(__file__).resolve().parents[2]


class DecisionJournalConfigurationTests(unittest.TestCase):
    def test_observer_and_api_share_the_journal_flag_and_postgresql_configuration(self):
        values = yaml.safe_load((ROOT / 'ansible/roles/control_plane/defaults/main.yml').read_text())
        values.update(control_plane_image='api:fixture', control_plane_incidents_enabled=True,
                      control_plane_decisions_enabled=True, control_plane_decision_journal_enabled=True,
                      control_plane_application_pg_database='moodle', pilotage_database_config_dir='/etc/moodle-pilotage',
                      pilotage_database_activation={'stat': {'exists': True}},
                      pilotage_auth_activation={'stat': {'exists': False}},
                      pilotage_policy_activation={'stat': {'exists': False}})
        environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
        environment.filters['bool'] = bool
        environment.filters['ternary'] = lambda value, yes, no: yes if value else no
        environment.filters['to_json'] = json.dumps
        document = yaml.safe_load(environment.from_string(
            (ROOT / 'ansible/roles/control_plane/templates/compose.yml.j2').read_text()).render(values))
        for service in ('api', 'observer'):
            configuration = document['services'][service]['environment']
            self.assertEqual(configuration['DECISION_JOURNAL_ENABLED'], 'true')
            self.assertEqual(configuration['PILOTAGE_DATABASE_CONFIG'], '/etc/moodle-pilotage/database.json')
        values['control_plane_decision_journal_enabled'] = False
        document = yaml.safe_load(environment.from_string(
            (ROOT / 'ansible/roles/control_plane/templates/compose.yml.j2').read_text()).render(values))
        self.assertEqual(document['services']['observer']['environment']['DECISION_JOURNAL_ENABLED'], 'false')

    def test_deployment_updates_backup_code_and_get_routes_are_bounded(self):
        play = yaml.safe_load((ROOT / 'ansible/playbooks/decision-journal.yml').read_text())[0]
        self.assertEqual(play['roles'], ['pilotage_backup', 'control_plane', 'dashboard'])
        for path in ('/api/v1/decisions/history', '/api/v1/decisions/history/1'):
            self.assertTrue(READ_PATH.fullmatch(path))
        for path in ('/api/v1/decisions/history/0', '/api/v1/decisions/history/1/execute', '/api/v1/decisions/history/execute'):
            self.assertFalse(READ_PATH.fullmatch(path))
