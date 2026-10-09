"""Les interruptions reseau sont bornees sans transformer un echec en succes."""
from contextlib import redirect_stderr, redirect_stdout
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

CI = Path(__file__).resolve().parents[1]
ROOT = CI.parent
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


class LoginTests(unittest.TestCase):
    def exercise(self, mode, *, token=True):
        workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
        blocks = [workflow['jobs'][name]['steps'][0]['run']
                  for name in ('recovery-tests', 'application-metrics', 'images')]
        self.assertEqual(blocks[0], blocks[1])
        self.assertEqual(blocks[0], blocks[2])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            docker = bin_dir / 'docker'
            docker.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
p=pathlib.Path(os.environ['CALLS']); calls=json.loads(p.read_text()) if p.exists() else []
assert sys.argv[1:] == ['login', '--username', 'fixture-user', '--password-stdin']
assert sys.stdin.read() == 'private-fixture-token'
assert pathlib.Path(os.environ['DOCKER_CONFIG']).stat().st_mode & 0o777 == 0o700
calls.append(len(calls)+1); p.write_text(json.dumps(calls))
if os.environ['MODE']=='success' or (os.environ['MODE']=='recover' and len(calls)==2):
    print('Login Succeeded'); sys.exit(0)
print('unauthorized: incorrect username or password' if os.environ['MODE']=='denied' else 'Client.Timeout exceeded while awaiting headers', file=sys.stderr)
sys.exit(1)
''')
            docker.chmod(0o755)
            sleep = bin_dir / 'sleep'
            sleep.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$DELAYS"\n')
            sleep.chmod(0o755)
            env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'],
                       RUNNER_TEMP=directory, GITHUB_ENV=str(root / 'github-env'),
                       DOCKERHUB_USERNAME='fixture-user',
                       DOCKERHUB_TOKEN='private-fixture-token' if token else '',
                       MODE=mode, CALLS=str(root / 'calls'), DELAYS=str(root / 'delays'))
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', blocks[0]],
                                    env=env, text=True, capture_output=True, timeout=15)
            calls = json.loads((root / 'calls').read_text()) if (root / 'calls').exists() else []
            delays = (root / 'delays').read_text().splitlines() if (root / 'delays').exists() else []
            github_env = (root / 'github-env').read_text() if (root / 'github-env').exists() else ''
            self.assertNotIn('private-fixture-token', result.stdout + result.stderr)
            self.assertFalse(list(root.glob('moodle-docker-login.*')))
            return result, calls, delays, github_env

    def test_network_failure_then_success_exports_configuration(self):
        result, calls, delays, exported = self.exercise('recover')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [1, 2])
        self.assertEqual(delays, ['5'])
        self.assertIn('DOCKER_CONFIG=', exported)

    def test_network_failure_stops_after_three_attempts(self):
        result, calls, delays, exported = self.exercise('timeout')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(delays, ['5', '10'])
        self.assertEqual(exported, '')

    def test_rejected_credentials_fail_without_retry(self):
        result, calls, delays, exported = self.exercise('denied')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [1])
        self.assertEqual(delays, [])
        self.assertEqual(exported, '')

    def test_missing_secret_fails_before_docker(self):
        result, calls, delays, exported = self.exercise('success', token=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])
        self.assertEqual(delays, [])
        self.assertEqual(exported, '')


if __name__ == '__main__':
    unittest.main()
