import unittest
from unittest.mock import Mock
from pilotage.operating_policy import PolicyBlocked,PolicyStorageError
from remediation.automatic import healthcheck, once


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.config={"api_url":"http://127.0.0.1:8000","policy_required":True}
        self.store=Mock();self.store.view.return_value={"policy":{"mode":"automatic","suspended":False}}
        self.store.reserve_worker.return_value={"revision":2,"sha256":"a"*64}
        self.reader=Mock(return_value={"status":"available","snapshot_usable":True,"decisions":[
            {"signal_key":"statistical_anomaly","status":"recommendation","incident_id":1},
            {"signal_key":"moodle_https","status":"recommendation","incident_id":5}]})
        self.executor=Mock(return_value=({"status":"completed","run_id":"c"*32},0))
    def run_once(self):return once(self.config,"/fixture/config",store=self.store,reader=self.reader,executor=self.executor)
    def test_only_confirmed_moodle_incident_reaches_existing_guarded_runner(self):
        result,code=self.run_once();self.assertEqual(code,0)
        self.assertEqual(self.executor.call_args.args[2],5)
        self.assertTrue(self.executor.call_args.kwargs["automatic"])
        self.assertEqual(self.executor.call_args.kwargs["expected_policy"]["revision"],2)
        self.assertEqual(self.store.reserve_worker.call_count,1)
    def test_suspended_or_nonautomatic_mode_does_not_even_read_incidents(self):
        for mode,suspended in (("observation",False),("assisted",False),("automatic",True)):
            self.store.view.return_value={"policy":{"mode":mode,"suspended":suspended}}
            self.assertEqual(self.run_once()[0]["status"],"idle")
        self.reader.assert_not_called();self.executor.assert_not_called()
    def test_evaluation_without_approval_is_blocked_and_never_dispatches(self):
        self.store.reserve_worker.side_effect=PolicyBlocked("evaluation_approval_required")
        self.assertEqual(self.run_once()[0]["reason"],"evaluation_approval_required")
        self.executor.assert_not_called()
    def test_a_previous_attempt_cannot_start_a_second_runner(self):
        self.store.reserve_worker.return_value=None
        self.assertEqual(self.run_once()[0]["status"],"idle");self.executor.assert_not_called()
    def test_unavailable_storage_and_unusable_data_do_not_execute(self):
        self.reader.return_value={"status":"available","snapshot_usable":False,"decisions":[]}
        self.assertEqual(self.run_once()[0]["status"],"blocked")
        self.store.view.side_effect=PolicyStorageError("fixture")
        self.assertEqual(self.run_once()[0]["status"],"unavailable")
        self.executor.assert_not_called()

    def test_guarded_refusal_is_a_normal_passage_and_does_not_retry(self):
        self.executor.return_value=({'status':'blocked','run_id':'c'*32},2)
        result,code=self.run_once()
        self.assertEqual((result['status'],code),('blocked',0))
        self.assertEqual(self.executor.call_count,1)

    def test_pool_profile_uses_same_guarded_executor_only_when_feature_is_enabled(self):
        self.reader.return_value['decisions'] = [{"signal_key":"php_pool_queue","status":"recommendation","incident_id":7}]
        self.assertEqual(self.run_once()[0]['status'],'idle'); self.executor.assert_not_called()
        self.config['php_profiles_enabled'] = True
        self.assertEqual(self.run_once()[0]['status'],'completed')
        self.assertEqual(self.executor.call_args.args[2],7)

    def test_healthcheck_never_reads_incidents_or_reserves_an_action(self):
        result,code=healthcheck(self.config,store=self.store)
        self.assertEqual((result["status"],code),("available",0))
        self.assertEqual(result["action_scope"],["start_php_stopped"])
        self.reader.assert_not_called();self.store.reserve_worker.assert_not_called();self.executor.assert_not_called()
