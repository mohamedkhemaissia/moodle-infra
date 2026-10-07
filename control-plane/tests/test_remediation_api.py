import sqlite3
import tempfile
import unittest
from fastapi.testclient import TestClient
from app.main import create_app
from app.remediation_store import RemediationStore


class RemediationAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.store = RemediationStore(self.tmp.name)
        self.store.initialize()
    def tearDown(self): self.tmp.cleanup()
    def test_persistent_action_reservation_report_and_readonly_routes(self):
        self.store.start('a'*32, 2, 1)
        self.store.reserve('a'*32, 2, 2, 'b'*64)
        self.store.finish('a'*32, 'completed', 3, report={'recovered':True}, decision={'action_id':'diagnose_moodle_https'})
        before = self.store.path.read_bytes()
        with TestClient(create_app(remediation_dir=self.tmp.name)) as client:
            summary = client.get('/api/v1/remediations/summary')
            self.assertEqual(summary.json()['mode'], 'manual_guarded')
            self.assertFalse(summary.json()['automatic_execution_enabled'])
            self.assertEqual(summary.headers['cache-control'], 'no-store')
            self.assertEqual(client.get('/api/v1/remediations?incident_id=2').json()['total'], 1)
            result = client.get('/api/v1/remediations/'+'a'*32).json()
            self.assertTrue(result['report']['recovered'])
            self.assertEqual(result['action_reservation']['container_id'], 'b'*64)
            self.assertEqual(result['action_id'], 'start_php_stopped')
            self.assertEqual(client.post('/api/v1/remediations', json={'incident_id':2}).status_code,405)
        self.assertEqual(before, self.store.path.read_bytes())
    def test_disabled_missing_and_unknown_are_explicit(self):
        with TestClient(create_app(remediation_dir='')) as client:
            self.assertEqual(client.get('/api/v1/remediations/summary').status_code,503)
        self.store.path.unlink()
        with TestClient(create_app(remediation_dir=self.tmp.name)) as client:
            self.assertEqual(client.get('/api/v1/remediations').status_code,503)
        self.assertFalse(self.store.path.exists())
    def test_invalid_params_and_unknown_id(self):
        with TestClient(create_app(remediation_dir=self.tmp.name)) as client:
            self.assertEqual(client.get('/api/v1/remediations/'+'f'*32).status_code,404)
            for path in ('/api/v1/remediations/invalid','/api/v1/remediations?limit=101','/api/v1/remediations?incident_id=0'):
                self.assertEqual(client.get(path).status_code,422)
