"""Contrats de deploiement des diagnostics guides et du pilote Codespaces."""
import json
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]


def role_name(value):
    return value if isinstance(value, str) else value["role"]


class DiagnosticRequestsConfigurationTests(unittest.TestCase):
    def test_api_dashboard_and_diagnostic_worker_sources_are_packaged(self):
        api = yaml.safe_load((ROOT / "ansible/roles/control_plane/defaults/main.yml").read_text())
        for name in (
            "pilotage/diagnostic_requests.py",
            "app/diagnostic_requests.py",
            "tests/test_diagnostic_requests_api.py",
        ):
            self.assertIn(name, api["control_plane_source_files"])
        dashboard = yaml.safe_load((ROOT / "ansible/roles/dashboard/defaults/main.yml").read_text())
        for name in (
            "src/DiagnosticRequestPanel.jsx",
            "src/diagnosticRequests.js",
            "tests/diagnosticRequests.test.js",
        ):
            self.assertIn(name, dashboard["dashboard_source_files"])
        diagnostics = yaml.safe_load((ROOT / "ansible/roles/diagnostics/defaults/main.yml").read_text())
        self.assertIn("worker.py", diagnostics["diagnostics_files"])
        self.assertIn("tests/test_diagnostic_worker.py", diagnostics["diagnostics_files"])

    def test_dedicated_role_initializes_postgresql_and_supports_both_schedulers(self):
        tasks = yaml.safe_load((ROOT / "ansible/roles/diagnostic_requests/tasks/main.yml").read_text())
        encoded = json.dumps(tasks)
        self.assertIn("pilotage.diagnostic_requests_cli", encoded)
        self.assertIn("--healthcheck", encoded)
        self.assertIn("diagnostic-requests-active", encoded)
        self.assertIn("ansible.builtin.systemd_service", encoded)
        self.assertIn("/usr/bin/supervisorctl", encoded)
        worker = (ROOT / "ansible/roles/diagnostic_requests/templates/moodle-diagnostic-requests-worker.j2").read_text()
        self.assertIn("PILOTAGE_DATABASE_CONFIG", worker)
        self.assertIn("PILOTAGE_SERVICE_TOKEN_FILE", worker)
        self.assertIn("-m diagnostics.worker", worker)

    def test_codespaces_playbook_excludes_backup_recovery_and_automatic_remediation(self):
        plays = yaml.safe_load((ROOT / "ansible/playbooks/pilot-codespaces.yml").read_text())
        roles = [role_name(role) for play in plays for role in play.get("roles", [])]
        for forbidden in (
            "linux", "runtime", "operations", "backup_status", "backup_catalog",
            "pilotage_backup", "recovery", "recovery_requests", "recovery_tests",
        ):
            self.assertNotIn(forbidden, roles)
        self.assertIn("diagnostic_requests", roles)
        values = yaml.safe_load((ROOT / "ansible/inventories/group_vars/codespaces/main.yml").read_text())
        self.assertFalse(values["remediation_worker_enabled"])
        self.assertFalse(values["remediation_php_profiles_enabled"])
        self.assertFalse(values["control_plane_backup_status_enabled"])
        self.assertFalse(values["control_plane_pilotage_backup_status_enabled"])
        self.assertFalse(values["control_plane_recovery_requests_enabled"])
        self.assertEqual(values["container_monitoring_scheduler"], "supervisor")
        self.assertEqual(values["diagnostic_requests_scheduler"], "supervisor")
        self.assertEqual(values["monitoring_node_exporter_mount_mode"], "ro")
        self.assertTrue(values["control_plane_diagnostic_requests_enabled"])
        encoded = json.dumps(values)
        self.assertIn("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", encoded)
        self.assertNotIn("app.github.dev", encoded)
        self.assertTrue(values["moodle_reverseproxy_enabled"])
        self.assertTrue(values["moodle_sslproxy_enabled"])
        moodle_config = (ROOT / "ansible/roles/moodle/templates/config.php.j2").read_text()
        self.assertIn("$CFG->reverseproxy", moodle_config)
        self.assertIn("$CFG->sslproxy", moodle_config)

    def test_devcontainer_uses_supported_private_by_default_port_attributes(self):
        document = json.loads((ROOT / ".devcontainer/devcontainer.json").read_text())
        self.assertTrue(document["privileged"])
        self.assertIn("ghcr.io/devcontainers/features/docker-in-docker:2", document["features"])
        self.assertEqual(document["portsAttributes"]["443"]["protocol"], "https")
        self.assertEqual(document["portsAttributes"]["8088"]["protocol"], "http")
        self.assertTrue(all("visibility" not in value for value in document["portsAttributes"].values()))
        self.assertEqual(document["postCreateCommand"], "bash scripts/codespaces/setup.sh")
        self.assertEqual(document["postStartCommand"], "bash scripts/codespaces/post-start.sh")
        status = (ROOT / "scripts/codespaces/status.sh").read_text()
        self.assertIn("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", status)
        self.assertIn("Origine dashboard: acceptee", status)

    def test_codespaces_deploy_uses_explicit_ansible_paths(self):
        script = (ROOT / "scripts/codespaces/deploy.sh").read_text()
        self.assertIn('export ANSIBLE_CONFIG="$repo_dir/ansible.cfg"', script)
        self.assertIn('export ANSIBLE_ROLES_PATH="$repo_dir/ansible/roles"', script)
        self.assertIn("ansible/inventories/codespaces.ini", script)
        self.assertIn("ansible/playbooks/pilot-codespaces.yml", script)
        self.assertIn("validate_bundle anomalies", script)
        self.assertIn("validate_bundle forecasts", script)
        self.assertIn('MOODLE_ANOMALY_BUNDLE_DIR=""', script)
        self.assertIn('MOODLE_FORECAST_BUNDLE_DIR=""', script)

    def test_codespaces_uses_venv_for_docker_modules(self):
        inventory = (ROOT / "ansible/inventories/codespaces.ini").read_text()
        self.assertNotIn("ansible_python_interpreter=/usr/bin/python3", inventory)
        values = yaml.safe_load((ROOT / "ansible/inventories/group_vars/codespaces/main.yml").read_text())
        self.assertEqual(values["ansible_python_interpreter"], "{{ moodle_repository_dir }}/.venv/bin/python")
        requirements = (ROOT / "scripts/codespaces/requirements.txt").read_text()
        self.assertIn("requests==", requirements)
        setup = (ROOT / "scripts/codespaces/setup.sh").read_text()
        self.assertIn("scripts/codespaces/requirements.txt", setup)

    def test_control_plane_database_migration_is_self_contained(self):
        defaults = yaml.safe_load((ROOT / "ansible/roles/control_plane_database/defaults/main.yml").read_text())
        for name in (
            "pilotage_database_migration_python",
            "pilotage_database_migration_code_dir",
            "pilotage_database_incident_source_dir",
            "pilotage_database_diagnostics_source_dir",
            "pilotage_database_remediation_source_dir",
        ):
            self.assertIn(name, defaults)
        tasks = (ROOT / "ansible/roles/control_plane_database/tasks/main.yml").read_text()
        for foreign_default in (
            "diagnostics_base_dir",
            "diagnostics_data_dir",
            "control_plane_incident_data_dir",
            "remediation_data_dir",
        ):
            self.assertNotIn(foreign_default, tasks)


if __name__ == "__main__":
    unittest.main()
