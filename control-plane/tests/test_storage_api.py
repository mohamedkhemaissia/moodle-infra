import tempfile
import unittest
import json
import os
import socket
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.incidents import IncidentStore
from app.diagnostics_store import DiagnosticStore
from app.remediation_store import RemediationStore
from app.main import create_app


class StorageTests(unittest.TestCase):
    def test_postgresql_unavailable_never_falls_back_to_preserved_sqlite_files(self):
        with tempfile.TemporaryDirectory() as root:
            stores = (IncidentStore(root), DiagnosticStore(root), RemediationStore(root))
            for store in stores:
                store.initialize()
            before = {str(store.path): store.path.read_bytes() for store in stores}
            secret = Path(root) / 'password'
            secret.write_text('a' * 64)
            secret.chmod(0o600)
            config = Path(root) / 'database.json'
            # Un port reserve mais sans serveur : aucune connexion a un vrai service.
            with socket.socket() as reserved:
                reserved.bind(('127.0.0.1', 0))
                config.write_text(json.dumps({'host': '127.0.0.1', 'port': reserved.getsockname()[1],
                    'dbname': 'moodle_pilotage', 'user': 'pilotage', 'password_file': str(secret)}))
                with patch.dict(os.environ, {'PILOTAGE_DATABASE_CONFIG': str(config)}):
                    with TestClient(create_app(incidents_dir=root, diagnostics_dir=root, remediation_dir=root)) as client:
                        self.assertEqual(client.get('/api/v1/storage/health').status_code, 503)
                        self.assertEqual(client.get('/api/v1/incidents/summary').status_code, 503)
                        self.assertEqual(client.get('/api/v1/health').status_code, 200)
            for store in stores:
                self.assertEqual(store.path.read_bytes(), before[str(store.path)])

    def test_storage_reports_real_readable_backends_without_exposing_paths(self):
        with tempfile.TemporaryDirectory() as root:
            for store in (IncidentStore(root), DiagnosticStore(root), RemediationStore(root)):
                store.initialize()
            with TestClient(create_app(incidents_dir=root, diagnostics_dir=root, remediation_dir=root)) as client:
                response = client.get('/api/v1/storage/health')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['cache-control'], 'no-store')
                self.assertEqual(set(response.json()['stores']), {'incidents', 'diagnostics', 'remediations'})
                self.assertEqual({value['backend'] for value in response.json()['stores'].values()}, {'sqlite'})
                self.assertNotIn(root, response.text)
                self.assertEqual(client.post('/api/v1/storage/health').status_code, 405)

    def test_missing_or_corrupt_database_is_not_reported_as_healthy(self):
        with TestClient(create_app(incidents_dir='', diagnostics_dir='', remediation_dir='')) as client:
            self.assertEqual(client.get('/api/v1/storage/health').status_code, 503)
            self.assertEqual(client.get('/api/v1/health').status_code, 200)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'incidents.sqlite3'
            path.write_bytes(b'invalid historical file')
            with TestClient(create_app(incidents_dir=root, diagnostics_dir='', remediation_dir='')) as client:
                self.assertEqual(client.get('/api/v1/storage/health').status_code, 503)
                self.assertEqual(path.read_bytes(), b'invalid historical file')
