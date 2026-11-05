"""Tests isoles du controle de socle du pilote Codespaces."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]


CURL_FIXTURE = r"""#!/usr/bin/env bash
set -euo pipefail

arguments="$*"
case "$arguments" in
  *127.0.0.1:8000/api/v1/health*) code="${MOCK_API_CODE:-200}" ;;
  *127.0.0.1:8088/api/v1/auth/login*) code="${MOCK_ORIGIN_CODE:-422}" ;;
  *127.0.0.1:8088/*) code="${MOCK_DASHBOARD_CODE:-200}" ;;
  *127.0.0.1:8443/login/index.php*) code="${MOCK_MOODLE_CODE:-200}" ;;
  *127.0.0.1:9090/-/ready*) code="${MOCK_PROMETHEUS_CODE:-200}" ;;
  *127.0.0.1:9093/-/ready*) code="${MOCK_ALERTMANAGER_CODE:-200}" ;;
  *) code="000" ;;
esac
printf '%s' "$code"
"""


SUDO_FIXTURE = r"""#!/usr/bin/env bash
set -euo pipefail

arguments="$*"
case "$arguments" in
  *moodle-diagnostic-requests-worker*--healthcheck*)
    [[ "${MOCK_DIAGNOSTIC_WORKER:-ok}" == "ok" ]] && printf 'healthy\n'
    ;;
  *moodle-remediation-worker*--healthcheck*)
    [[ "${MOCK_REMEDIATION_WORKER:-ok}" == "ok" ]] && printf 'healthy\n'
    ;;
  *moodle-fpm-profiles*--status*)
    [[ "${MOCK_FPM_PROFILES:-ok}" == "ok" ]] && printf 'Normal\n'
    ;;
  *moodle-site-operations-worker*--healthcheck*)
    [[ "${MOCK_SITE_WORKER:-ok}" == "ok" ]] && printf 'healthy\n'
    ;;
  *moodle-cron-last-success*)
    [[ "${MOCK_CRON:-ok}" == "ok" ]]
    ;;
  *course-operations*worker.php*--healthcheck*)
    [[ "${MOCK_COURSE_WORKER:-ok}" == "ok" ]]
    ;;
  *supervisorctl*status*)
    [[ "${MOCK_SUPERVISOR:-ok}" == "ok" ]] || exit 1
    printf '%s\n' \
      'moodle-container-metrics RUNNING' \
      'moodle-diagnostic-requests RUNNING' \
      'moodle-remediation-worker RUNNING' \
      'moodle-site-operations RUNNING'
    ;;
  *)
    printf 'Commande sudo inattendue: %s\n' "$arguments" >&2
    exit 2
    ;;
esac
"""


class CodespacesStatusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix=".status-tests-", dir=ROOT
        )
        self.test_root = Path(self.temporary.name)
        self.bin_dir = self.test_root / "bin"
        self.home_dir = self.test_root / "home"
        self.bin_dir.mkdir()
        self.home_dir.mkdir()
        self._write_executable("curl", CURL_FIXTURE)
        self._write_executable("sudo", SUDO_FIXTURE)

    def tearDown(self):
        self.temporary.cleanup()

    def _write_executable(self, name: str, content: str) -> None:
        target = self.bin_dir / name
        target.write_text(textwrap.dedent(content), encoding="utf-8", newline="\n")
        target.chmod(0o755)

    @staticmethod
    def _bash() -> str:
        if os.name == "nt":
            git_bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
            if git_bash.is_file():
                return str(git_bash)
        executable = shutil.which("bash")
        if executable is None:
            raise unittest.SkipTest("Bash n'est pas disponible.")
        return executable

    def run_status(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            CODESPACE_NAME="pfe-test",
            GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN="example.test",
            STATUS_TEST_BIN=self.bin_dir.relative_to(ROOT).as_posix(),
            STATUS_TEST_HOME=self.home_dir.relative_to(ROOT).as_posix(),
            **overrides,
        )
        command = (
            'PATH="$PWD/$STATUS_TEST_BIN:$PATH"; '
            'HOME="$PWD/$STATUS_TEST_HOME"; '
            "export PATH HOME; bash scripts/codespaces/status.sh"
        )
        return subprocess.run(
            [self._bash(), "-c", command],
            cwd=ROOT,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

    def test_healthy_pilot_is_successful(self):
        result = self.run_status()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Tableau de bord: OK (HTTP 200)", result.stdout)
        self.assertIn("Moodle derriere le proxy: OK (HTTP 200)", result.stdout)
        self.assertIn("Prometheus: OK (HTTP 200)", result.stdout)
        self.assertIn("Cron Moodle: actif", result.stdout)
        self.assertIn("Worker sauvegarde/restauration de cours: disponible", result.stdout)
        self.assertIn("Bilan: OK", result.stdout)

    def test_moodle_redirect_is_a_failure(self):
        result = self.run_status(MOCK_MOODLE_CODE="302")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("redirection HTTP 302 refusee", result.stdout)
        self.assertIn("Bilan: ECHEC", result.stdout)

    def test_inactive_cron_is_a_failure(self):
        result = self.run_status(MOCK_CRON="failed")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Cron Moodle: ECHEC", result.stdout)
        self.assertIn("Bilan: ECHEC", result.stdout)

    def test_unavailable_course_worker_is_a_failure(self):
        result = self.run_status(MOCK_COURSE_WORKER="failed")

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Worker sauvegarde/restauration de cours: ECHEC", result.stdout)
        self.assertIn("Bilan: ECHEC", result.stdout)


if __name__ == "__main__":
    unittest.main()
