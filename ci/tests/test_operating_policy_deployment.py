"""Exercise the deployment gate using the actual Ansible condition."""
from copy import deepcopy
from pathlib import Path
import unittest

import jinja2
import yaml


ROOT = Path(__file__).resolve().parents[2]


class RemediationDeploymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tasks = yaml.safe_load(
            (ROOT / "ansible/roles/control_plane/tasks/main.yml").read_text()
        )
        cls.task = next(
            task for task in tasks
            if task.get("register") == "control_plane_remediation_response"
        )
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        cls.condition = staticmethod(env.compile_expression(cls.task["until"]))

    def accepts(self, response, policies_active):
        return self.condition(
            control_plane_remediation_response=response,
            pilotage_policy_activation={"stat": {"exists": policies_active}},
        )

    @staticmethod
    def policy_response(mode="observation", suspended=False, enabled=False):
        return {"status": 200, "json": {
            "status": "available", "mode": "policy_guarded",
            "automatic_execution_enabled": enabled,
            "operating_policy": {"mode": mode, "suspended": suspended},
        }}

    def test_existing_manual_deployment_remains_usable(self):
        response = {"status": 200, "json": {
            "status": "available", "mode": "manual_guarded",
            "automatic_execution_enabled": False,
        }}
        self.assertTrue(self.accepts(response, False))
        self.assertFalse(self.accepts(response, True))
        response["json"]["automatic_execution_enabled"] = True
        self.assertFalse(self.accepts(response, False))

    def test_each_policy_mode_accepts_its_documented_execution_flag(self):
        cases = (
            ("observation", False, False), ("observation", True, False),
            ("assisted", False, False), ("assisted", True, False),
            ("automatic", False, True), ("automatic", True, False),
        )
        for mode, suspended, enabled in cases:
            with self.subTest(mode=mode, suspended=suspended):
                response = self.policy_response(mode, suspended, enabled)
                self.assertTrue(self.accepts(response, True))
                self.assertFalse(self.accepts(response, False))
                response["json"]["automatic_execution_enabled"] = not enabled
                self.assertFalse(self.accepts(response, True))

    def test_http_errors_missing_json_and_unavailable_storage_are_rejected(self):
        for response in ({}, {"status": 503}, {"status": 200},
                         {"status": 200, "json": None},
                         {"status": 200, "json": []}):
            for active in (False, True):
                with self.subTest(response=response, active=active):
                    self.assertFalse(self.accepts(response, active))
        response = self.policy_response()
        response["json"]["status"] = "unavailable"
        self.assertFalse(self.accepts(response, True))
        response = self.policy_response()
        response["status"] = 503
        self.assertFalse(self.accepts(response, True))

    def test_missing_or_invalid_policy_does_not_pass_the_deployment_gate(self):
        for policy in (None, {}, [], {"mode": "unexpected", "suspended": False},
                       {"mode": "automatic"},
                       {"mode": "automatic", "suspended": "false"},
                       {"mode": "automatic", "suspended": 0}):
            response = self.policy_response()
            response["json"]["operating_policy"] = policy
            with self.subTest(policy=policy):
                self.assertFalse(self.accepts(response, True))
        response = self.policy_response()
        del response["json"]["operating_policy"]
        self.assertFalse(self.accepts(response, True))

    def test_missing_or_non_boolean_execution_flags_are_rejected(self):
        for value in (None, "false", "true", 0, 1):
            response = self.policy_response()
            response["json"]["automatic_execution_enabled"] = value
            with self.subTest(value=value):
                self.assertFalse(self.accepts(response, True))
        response = self.policy_response()
        del response["json"]["automatic_execution_enabled"]
        self.assertFalse(self.accepts(response, True))

    def test_missing_status_or_unexpected_api_mode_is_rejected(self):
        original = self.policy_response()
        for field in ("status", "mode"):
            response = deepcopy(original)
            del response["json"][field]
            self.assertFalse(self.accepts(response, True))
        response = self.policy_response()
        response["json"]["mode"] = "automatic"
        self.assertFalse(self.accepts(response, True))

    def test_authenticated_probe_keeps_secrets_hidden(self):
        self.assertEqual(self.task["no_log"], "{{ pilotage_auth_activation.stat.exists }}")
        self.assertEqual(self.task["ansible.builtin.uri"]["status_code"], 200)
        self.assertEqual(self.task["ansible.builtin.uri"]["headers"],
                         "{{ control_plane_read_headers }}")


if __name__ == "__main__":
    unittest.main()
