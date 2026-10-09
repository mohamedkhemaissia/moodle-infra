"""Les interruptions reseau sont bornees sans transformer un echec en succes."""
from contextlib import redirect_stderr, redirect_stdout
import io
import importlib.util
from pathlib import Path
import sys
import subprocess
import unittest

import yaml

CI = Path(__file__).resolve().parents[1]
ROOT = CI.parent
sys.path.insert(0, str(CI))
SPEC = importlib.util.spec_from_file_location('pull_ci_images', CI / 'pull_ci_images.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PullTests(unittest.TestCase):
    def exercise(self, outcomes):
        calls, delays = [], []

        def run(command, **options):
            calls.append((command, options))
            value = outcomes[len(calls) - 1]
            if isinstance(value, BaseException):
                raise value
            return subprocess.CompletedProcess(command, 0, stdout='downloaded', stderr='')

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                MODULE.pull_image('postgres:fixture@sha256:' + 'a' * 64, run=run, sleep=delays.append)
                error = None
            except BaseException as exc:
                error = exc
        return calls, delays, error

    def failure(self, message):
        return subprocess.CalledProcessError(1, ['docker', 'pull'], stderr=message)

    def test_transient_failure_then_success_uses_only_pull_and_exact_reference(self):
        calls, delays, error = self.exercise([self.failure('Client.Timeout exceeded while awaiting headers'), None])
        self.assertIsNone(error)
        self.assertEqual(len(calls), 2)
        self.assertEqual(delays, [5])
        for command, options in calls:
            self.assertEqual(command, ['docker', 'pull', 'postgres:fixture@sha256:' + 'a' * 64])
            self.assertEqual(options['timeout'], 120)
            self.assertTrue(options['check'])

    def test_permanent_failures_are_not_retried_even_when_timeout_text_is_present(self):
        for message in ('unauthorized: timeout', 'manifest unknown: deadline exceeded',
                        'toomanyrequests: TLS handshake timeout', 'invalid reference format'):
            with self.subTest(message=message):
                calls, delays, error = self.exercise([self.failure(message)])
                self.assertEqual(len(calls), 1)
                self.assertEqual(delays, [])
                self.assertIsInstance(error, subprocess.CalledProcessError)

    def test_third_failure_is_preserved_and_no_fourth_attempt_occurs(self):
        outcomes = [self.failure('connection reset by peer') for _ in range(3)]
        calls, delays, error = self.exercise(outcomes)
        self.assertEqual(len(calls), 3)
        self.assertEqual(delays, [5, 10])
        self.assertIs(error, outcomes[-1])

    def test_subprocess_timeout_with_bytes_is_bounded(self):
        expired = subprocess.TimeoutExpired(['docker', 'pull'], 120, stderr=b'partial')
        calls, delays, error = self.exercise([expired, expired, expired])
        self.assertEqual(len(calls), 3)
        self.assertEqual(delays, [5, 10])
        self.assertIs(error, expired)

    def test_unrelated_failure_is_not_retried(self):
        calls, delays, error = self.exercise([self.failure('Cannot connect to the Docker daemon')])
        self.assertEqual(len(calls), 1)
        self.assertFalse(delays)
        self.assertIsInstance(error, subprocess.CalledProcessError)

    def test_selected_images_keep_the_references_from_the_project(self):
        moodle = yaml.safe_load((ROOT / 'ansible/roles/moodle/defaults/main.yml').read_text())
        for profile in ('recovery-tests', 'application-metrics'):
            selected = MODULE.images_for(profile)
            self.assertIn(moodle['moodle_postgres_image'], selected)
            self.assertIn(moodle['moodle_php_base_image'], selected)
            self.assertEqual(len(selected), len(set(selected)))
        self.assertIn(moodle['moodle_apache_image'], MODULE.images_for('recovery-tests'))
        with self.assertRaises(ValueError):
            MODULE.images_for('production')



if __name__ == '__main__':
    unittest.main()
