import unittest
from app.decisions import policy_document
from diagnostics.policy import POLICY_SHA, POLICY_VERSION


class SharedPolicyTests(unittest.TestCase):
    def test_diagnostics_and_corrections_accept_the_exact_decision_policy(self):
        policy = policy_document()
        self.assertEqual((policy["version"], policy["sha256"]), (POLICY_VERSION, POLICY_SHA))
