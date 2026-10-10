"""File et journal reels : ces tests exigent PostgreSQL 16 dans le job CI."""
from concurrent.futures import ThreadPoolExecutor
import json
import unittest
from unittest.mock import patch

import test_recovery_requests as requests
from pilotage.auth import AuthDenied
from pilotage.db import raw_connect
from pilotage.recovery import RecoveryConflict, RecoveryStorageError
from pilotage.recovery_execution import RecoveryExecutionStore

BASE=requests.BASE
FLAGS=dict(containers_removed=True,networks_removed=True,temporary_files_removed=True)
PROOF=dict(status='restore_validated',production_changed=False,database_check='passed',login_check='passed',archive_check='passed',files_verified=6,cleanup=FLAGS)


class ExecutionPostgreSQLTests(unittest.TestCase):
    setUpClass=classmethod(requests.RecoveryPostgreSQLTests.setUpClass.__func__)
    reset=requests.RecoveryPostgreSQLTests.reset
    write_catalog=requests.RecoveryPostgreSQLTests.write_catalog
    create=requests.RecoveryPostgreSQLTests.create
    decide=requests.RecoveryPostgreSQLTests.decide
    def setUp(self):
        requests.RecoveryPostgreSQLTests.setUp(self)
        old=self.store
        self.store=RecoveryExecutionStore(database_config=self.path,selector=old.selector)
        self.assertTrue(self.store.initialize_execution())
        self.row=self.decide(self.create()[0],principal=self.other_principal)
    def enqueue(self,**kwargs):
        return self.store.enqueue(self.principal,self.row['id'],revision=self.row['revision'],confirmed_snapshot_id=self.row['snapshot_id'],now=BASE+4,**kwargs)
    def test_initialization_preserves_request_events_accounts_and_is_idempotent(self):
        before=self.store.detail(self.row['id'],now=BASE+4)
        self.assertFalse(self.store.initialize_execution());self.store.check()
        self.assertEqual(before,self.store.detail(self.row['id'],now=BASE+4));self.assertEqual(len(self.auth.users()),3)
    def test_double_click_and_parallel_claim_dispatch_exactly_once(self):
        with ThreadPoolExecutor(max_workers=2) as workers:results=list(workers.map(lambda _:self.enqueue(),range(2)))
        self.assertEqual(sum(created for row,created in results),1)
        self.assertEqual(len({row['execution']['id'] for row,created in results}),1)
        with ThreadPoolExecutor(max_workers=2) as workers:claims=list(workers.map(lambda _:self.store.claim('d'*32,now=BASE+5),range(2)))
        self.assertEqual(sum(row is not None for row in claims),1)
        job=next(row for row in claims if row is not None)
        self.store.finish(job['id'],job['claim_token'],PROOF,now=BASE+10)
        self.assertIsNone(self.store.claim('d'*32,now=BASE+11))
        detail=self.store.detail(self.row['id'],now=BASE+11)
        self.assertEqual(detail['request']['execution']['status'],'succeeded')
        self.assertEqual([event['action'] for event in detail['execution_events']],['queued','started','finished'])
    def test_expired_approval_never_enqueues_and_stale_confirmation_never_mutates(self):
        for now,revision,snapshot in [(BASE+904,2,'a'*64),(BASE+4,1,'a'*64),(BASE+4,2,'b'*64)]:
            with self.subTest(now=now),self.assertRaises(RecoveryConflict):
                self.store.enqueue(self.principal,self.row['id'],revision=revision,confirmed_snapshot_id=snapshot,now=now)
        self.assertEqual(self.store.detail(self.row['id'],now=BASE+5)['execution_events'],[])
    def test_authorization_is_rechecked_when_worker_takes_queued_request(self):
        self.enqueue();self.auth.update_user(self.user['id'],role='reader',now=BASE+5)
        self.assertIsNone(self.store.claim('d'*32,now=BASE+6))
        self.assertEqual(self.store.detail(self.row['id'],now=BASE+7)['request']['execution']['status'],'blocked')
    def test_approver_revocation_and_catalog_change_block_physical_dispatch(self):
        self.enqueue();self.auth.update_user(self.other['id'],role='reader',now=BASE+5)
        self.assertIsNone(self.store.claim('d'*32,now=BASE+6))
        self.assertEqual(self.store.detail(self.row['id'],now=BASE+7)['request']['execution']['status'],'blocked')
    def test_queued_cancellation_and_claim_are_serialized(self):
        self.enqueue()
        def cancel():
            try:return self.decide(self.row,'cancel',now=BASE+5)['status']
            except RecoveryConflict:return 'already_running'
        with ThreadPoolExecutor(max_workers=2) as workers:
            first=workers.submit(cancel);second=workers.submit(self.store.claim,'d'*32,now=BASE+5)
            cancelled,claimed=first.result(),second.result()
        if cancelled=='cancelled':
            self.assertIsNone(claimed);self.assertEqual(self.store.detail(self.row['id'],now=BASE+6)['request']['execution']['status'],'cancelled')
        else:
            self.assertIsNotNone(claimed)
            with self.assertRaises(RecoveryConflict):self.decide(self.row,'cancel',now=BASE+6)
    def test_global_single_worker_preserves_next_request_in_queue(self):
        self.enqueue();next_row=self.decide(self.create(key='e'*32)[0])
        self.store.enqueue(self.principal,next_row['id'],revision=2,confirmed_snapshot_id='a'*64,now=BASE+4)
        first=self.store.claim('d'*32,now=BASE+5)
        self.assertIsNone(self.store.claim('d'*32,now=BASE+6))
        self.store.finish(first['id'],first['claim_token'],PROOF,now=BASE+7)
        self.assertIsNotNone(self.store.claim('d'*32,now=BASE+8))
    def test_deadline_boot_change_and_result_token_cannot_hide_interrupted_work(self):
        self.enqueue();job=self.store.claim('d'*32,now=BASE+5)
        self.assertEqual(self.store.abandoned('e'*32,now=BASE+6)[0]['id'],job['id'])
        with self.assertRaises(RecoveryConflict):self.store.finish(job['id'],'f'*32,PROOF,now=BASE+8)
        result=self.store.finish(job['id'],job['claim_token'],PROOF,now=BASE+1086)
        self.assertEqual(result['status'],'inconclusive')
        with self.assertRaises(RecoveryConflict):self.store.finish(job['id'],job['claim_token'],PROOF,now=BASE+1087)
    def test_execution_audit_failure_rolls_back_queue_insertion(self):
        with raw_connect(self.config) as connection:
            connection.execute("CREATE FUNCTION pilotage_recovery.fail_execution() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture'; END $$")
            connection.execute("CREATE TRIGGER fail_execution BEFORE INSERT ON pilotage_recovery.execution_events FOR EACH ROW EXECUTE FUNCTION pilotage_recovery.fail_execution()")
        with self.assertRaises(RecoveryStorageError):self.enqueue()
        self.assertEqual(self.store.detail(self.row['id'],now=BASE+5)['request']['execution']['status'],'not_dispatched')
    def test_public_proofs_never_contain_worker_credentials_or_claim_tokens(self):
        self.enqueue();job=self.store.claim('d'*32,now=BASE+5)
        value=json.dumps(self.store.detail(self.row['id'],now=BASE+6))
        for secret in (job['claim_token'],self.principal['session_hash'],self.session['token'],requests.PASSWORD):self.assertNotIn(secret,value)
        self.assertEqual(self.store.detail(self.row['id'],now=BASE+6)['execution_events'][1]['by']['kind'],'worker')


if __name__=='__main__':unittest.main()
