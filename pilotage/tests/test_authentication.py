"""Cryptographie reelle, chemins d'exploitation et interface locale sans serveur de production."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from argon2 import extract_parameters, Type
from pilotage.auth import AuthStore, AuthStorageError, csrf_token, session_hash, timestamp, username
from pilotage.auth_cli import password
from pilotage.passwords import hash_password, verify_password
from pilotage.worker_auth import worker_headers, read_secret_file


class PasswordTests(unittest.TestCase):
    def test_argon2id_has_random_salt_and_rejects_wrong_password_and_unbounded_hash_cost(self):
        value = 'phrase-secrete-de-test-seulement'
        first, second = hash_password(value), hash_password(value)
        self.assertNotEqual(first, second)
        parameters = extract_parameters(first)
        self.assertEqual(parameters.type, Type.ID)
        self.assertEqual((parameters.memory_cost, parameters.time_cost, parameters.parallelism), (19456, 2, 1))
        self.assertTrue(verify_password(first, value))
        for encoded, candidate in ((first, 'incorrect'), (first.replace('m=19456', 'm=999999999'), value),
                                   ('broken', value), (None, value), (first, 'x' * 129)):
            self.assertFalse(verify_password(encoded, candidate))

    def test_password_and_identity_constraints_do_not_expose_the_input(self):
        for value in ('short', 'x' * 129, None, 'x' * 16 + '\x00'):
            with self.assertRaises(ValueError):
                hash_password(value)
        self.assertEqual(username(' HAMMA '), 'hamma')
        for value in (None, 'a', 'a/b', 'a' * 33):
            with self.assertRaises(ValueError):
                username(value)
        for value in (-1, float('nan'), float('inf'), True):
            with self.assertRaises(ValueError):
                timestamp(value)
        self.assertNotEqual(session_hash('a' * 43), csrf_token('a' * 43))

    def test_accounts_never_fall_back_to_sqlite_when_configuration_is_absent_or_bad(self):
        for config in ('', '/does-not-exist/database.json'):
            with self.assertRaises(AuthStorageError):
                AuthStore(database_config=config)

    def test_local_password_entry_is_interactive_and_confirms_without_output(self):
        with patch('pilotage.auth_cli.sys.stdin.isatty', return_value=False), patch('pilotage.auth_cli.getpass.getpass') as prompt:
            with self.assertRaises(ValueError):
                password()
            prompt.assert_not_called()
        with patch('pilotage.auth_cli.sys.stdin.isatty', return_value=True), patch('pilotage.auth_cli.getpass.getpass', side_effect=['a', 'b']):
            with self.assertRaises(ValueError):
                password()


class WorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.secret = Path(temporary.name) / 'worker-token'
        self.secret.write_text('c' * 64 + '\n')
        self.secret.chmod(0o600)
        environment = patch.dict(os.environ, {'PILOTAGE_SERVICE_TOKEN_FILE': str(self.secret)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_secret_only_goes_to_allowlisted_loopback_pilotage_reads(self):
        expected = {'Authorization': 'Bearer ' + 'c' * 64}
        for path in ('metrics/summary', 'incidents?status=open', 'incidents/1/events', 'decisions/1',
                     'remediations/' + 'a' * 32):
            self.assertEqual(worker_headers('http://127.0.0.1:8000/api/v1/' + path), expected)
        for url in ('http://127.0.0.1:9090/api/v1/query?query=up',
                    'https://external.example/api/v1/users', 'http://127.0.0.1:8000/api/v1/auth/session'):
            self.assertEqual(worker_headers(url), {})
        for url in ('https://external.example/api/v1/incidents', 'http://localhost:8000/api/v1/incidents',
                    'http://127.0.0.1/api/v1/incidents', 'http://user@127.0.0.1:8000/api/v1/incidents',
                    'http://127.0.0.1:8000/api/v1/incidents#x'):
            with self.assertRaises(ValueError):
                worker_headers(url)

    def test_public_file_symlinks_and_malformed_secret_fail_closed(self):
        self.secret.chmod(0o644)
        with self.assertRaises(ValueError):
            read_secret_file(self.secret)
        self.secret.chmod(0o600)
        link = self.secret.parent / 'link'
        link.symlink_to(self.secret)
        with self.assertRaises(ValueError):
            read_secret_file(link)
        self.secret.write_text('invalid')
        with self.assertRaises(ValueError):
            worker_headers('http://127.0.0.1:8000/api/v1/metrics/summary')

    def test_explicit_legacy_mode_cannot_accidentally_attach_a_secret(self):
        with patch.dict(os.environ, {'PILOTAGE_SERVICE_TOKEN_FILE': ''}):
            self.assertEqual(worker_headers('http://127.0.0.1:8000/api/v1/metrics/summary'), {})
