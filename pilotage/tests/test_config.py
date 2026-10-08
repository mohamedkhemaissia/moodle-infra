import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sqlite3

from pilotage.db import load_config
from pilotage.migrate import MigrationError, activate, source_snapshot


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.secret = self.base / 'password'
        self.secret.write_text('a' * 64 + '\n')
        self.secret.chmod(0o640)
        self.path = self.base / 'database.json'
        self.value = {'host': '127.0.0.1', 'port': 15432, 'dbname': 'moodle_pilotage',
                      'user': 'pilotage', 'password_file': str(self.secret)}
        self.write()
    def write(self):
        self.path.write_text(json.dumps(self.value))
    def test_explicit_configuration_and_secret_file_are_required(self):
        self.assertIsNone(load_config(''))
        loaded = load_config(str(self.path))
        self.assertEqual(loaded['password'], 'a' * 64)
        self.assertNotIn('password_file', loaded)
    def test_invalid_configuration_does_not_fall_back_to_sqlite_or_disclose_secret(self):
        for field, value in [('host', 'database.example'), ('port', True), ('user', "user';--"),
                             ('password_file', 'relative-path')]:
            with self.subTest(field=field):
                original = self.value[field]
                self.value[field] = value
                self.write()
                with self.assertRaises(sqlite3.OperationalError) as error:
                    load_config(str(self.path))
                self.assertNotIn('a' * 64, str(error.exception))
                self.value[field] = original
        self.write()
        self.secret.chmod(0o644)
        with self.assertRaises(sqlite3.OperationalError):
            load_config(str(self.path))
    def test_duplicate_keys_symlinks_and_extra_parameters_are_rejected(self):
        self.path.write_text('{"host":"127.0.0.1","host":"127.0.0.1"}')
        with self.assertRaises(sqlite3.OperationalError):
            load_config(str(self.path))
        self.value['password'] = 'inline-secret'
        self.write()
        with self.assertRaises(sqlite3.OperationalError):
            load_config(str(self.path))
        alias = self.base / 'alias.json'
        alias.symlink_to(self.path)
        with self.assertRaises(sqlite3.OperationalError):
            load_config(str(alias))
    def test_activation_is_atomic_and_different_marker_is_never_replaced(self):
        marker = self.base / 'active'
        activate(marker, '1' * 64)
        before = marker.read_bytes()
        activate(marker, '1' * 64)
        with self.assertRaises(MigrationError):
            activate(marker, '2' * 64)
        self.assertEqual(marker.read_bytes(), before)
        self.assertEqual(list(self.base.glob('.activation-*')), [])
    def test_active_observer_blocks_snapshot_before_database_access(self):
        dirs = {key: self.base / key for key in ('incidents', 'diagnostics', 'remediation')}
        for directory in dirs.values():
            directory.mkdir()
        with (dirs['incidents'] / 'observer.lock').open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(MigrationError):
                with source_snapshot(dirs):
                    self.fail('Snapshot avec un ecrivain actif')


if __name__ == '__main__':
    unittest.main()
