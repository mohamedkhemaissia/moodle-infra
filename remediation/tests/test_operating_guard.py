import unittest
from unittest.mock import patch,Mock
from pilotage.operating_policy import PolicyBlocked,PolicyStorageError
import test_remediation as fixture


class OperatingGuardTests(unittest.TestCase):
    setUp=fixture.ActionTests.setUp
    tearDown=fixture.ActionTests.tearDown
    json=fixture.ActionTests.json
    inspect=fixture.ActionTests.inspect
    probe=fixture.ActionTests.probe
    start=fixture.ActionTests.start
    run_action=fixture.ActionTests.run_action
    def test_denied_policy_never_dispatches_docker_or_reserves_remediation(self):
        self.config["policy_required"]=True
        for reason in ("observation_mode","policy_suspended","evaluation_approval_required","operating_policy_changed"):
            with self.subTest(reason=reason),patch("remediation.action.PolicyStore") as store:
                store.return_value.dispatch.side_effect=PolicyBlocked(reason)
                result=self.run_action()
                self.assertEqual(result["reason"],reason);self.assertFalse(result["action_dispatched"])
                self.assertEqual(self.calls,[]);self.assertIsNone(self.store.detail(self.identifier)["action_reservation"])
    def test_storage_failure_cannot_fall_back_to_ungoverned_action(self):
        self.config["policy_required"]=True
        with patch("remediation.action.PolicyStore",side_effect=PolicyStorageError("fixture")):
            result=self.run_action()
        self.assertEqual(result["status"],"blocked");self.assertEqual(self.calls,[])
    def test_authorization_is_committed_before_docker_receives_the_start(self):
        self.config["policy_required"]=True
        policy={"revision":2,"sha256":"a"*64}
        self.expected["operating_policy"]=policy;self.expected["execution_mode"]="automatic_guarded"
        authorized=[]
        def dispatch(document,expected,*,reserve,**kwargs):
            self.assertEqual(expected,policy);self.assertTrue(kwargs["automatic"])
            self.assertIsNone(reserve());authorized.append(kwargs["run_id"])
            return {"policy":policy,"trigger_mode":"automatic","approval_id":None}
        def start(identifier):
            self.assertEqual(authorized,[self.identifier]);return fixture.ActionTests.start(self,identifier)
        with patch("remediation.action.PolicyStore") as store:
            store.return_value.dispatch.side_effect=dispatch
            result=self.run_action(start_fn=start)
        self.assertEqual(result["status"],"completed");self.assertEqual(result["mode"],"automatic_guarded")
        self.assertEqual(result["operating_authorization"]["policy"],policy)
    def test_automatic_cannot_use_legacy_configuration_without_policies(self):
        self.expected["execution_mode"]="automatic_guarded"
        result=self.run_action()
        self.assertEqual(result["reason"],"automatic_requires_operating_policy");self.assertEqual(self.calls,[])
