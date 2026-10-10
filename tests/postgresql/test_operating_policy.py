"""Verrous, autorisations et audit sur le PostgreSQL isole deja utilise par la CI."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
import unittest
from unittest.mock import patch

from pilotage.auth import AuthStore
from pilotage.db import load_config, raw_connect
from pilotage.operating_policy import PolicyStore, PolicyBlocked, PolicyConflict, PolicyStorageError

BASE=1791547200.0
PASSWORD='phrase-de-test-sans-compte-reel'


def document(incident=5):
    iso=lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
    return {'status':'available','snapshot_usable':True,'mode':'simulation',
        'decision':{'incident_id':incident,'signal_key':'moodle_https','status':'recommendation',
            'incident_status':'open','latest_state':'triggered','evidence':{'probe_success':False},
            'recommended_action':{'id':'diagnose_moodle_https'},'decision_id':'c'*64,'policy_sha256':'b'*64,
            'context':'policy-v1','input_evaluated_at':iso(BASE),'valid_until':iso(BASE+90)}}


class OperatingPolicyPostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path=os.environ.get('TEST_POSTGRES_CONFIG')
        if not cls.path:raise RuntimeError('TEST_POSTGRES_CONFIG requis ; aucune base de production.')
        cls.config=load_config(cls.path)

    def reset(self):
        with raw_connect(self.config) as connection:
            connection.execute('DROP SCHEMA IF EXISTS pilotage_policy CASCADE')
            connection.execute('DROP SCHEMA IF EXISTS pilotage_auth CASCADE')

    def setUp(self):
        self.reset();self.addCleanup(self.reset)
        self.auth=AuthStore(database_config=self.path);self.auth.initialize()
        self.user=self.auth.add_user('hamma','Hamma','admin',PASSWORD,now=BASE)
        self.session=self.auth.login('hamma',PASSWORD,now=BASE)
        self.principal={**self.auth.authenticate(self.session['token'],now=BASE+1),'kind':'human'}
        self.store=PolicyStore(database_config=self.path)
        with patch('pilotage.operating_policy.time.time',return_value=BASE-10):
            self.assertTrue(self.store.initialize())

    def update(self,**changes):
        kwargs=dict(revision=1,mode='assisted',suspended=False,evaluation_context=False,
            reason='Configurer le pilotage pour ce test',confirm_automatic=False,now=BASE+2)
        kwargs.update(changes)
        return self.store.update(self.principal,**kwargs)

    def approve(self,**changes):
        kwargs=dict(incident_id=5,revision=2,reason='Autoriser la reprise pendant cette evaluation',client_key='d'*32,now=BASE+3)
        kwargs.update(changes)
        return self.store.approve(self.principal,document(),**kwargs)

    def dispatch(self,policy,**changes):
        kwargs=dict(run_id='e'*32,automatic=False,reserve=lambda:None,now=BASE+5)
        kwargs.update(changes)
        return self.store.dispatch(document(),policy,**kwargs)

    def test_initialization_preserves_accounts_and_does_not_activate_automatic(self):
        users=self.auth.users()
        self.assertFalse(self.store.initialize())
        self.assertEqual(self.auth.users(),users)
        self.assertEqual(self.store.view(now=BASE)['policy']['mode'],'observation')
        with self.assertRaises(PolicyBlocked):self.dispatch(self.store.view(now=BASE)['policy'])

    def test_parallel_admin_updates_yield_one_revision_and_one_audit_event(self):
        def change(_):
            try:self.update();return 'changed'
            except PolicyConflict:return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(change,range(2)))
        self.assertEqual(sorted(results),['changed','conflict'])
        self.assertEqual(self.store.view(now=BASE+3)['policy']['revision'],2)
        self.assertEqual(self.store.history()['total'],1)

    def test_evaluation_approval_is_bound_to_revision_and_consumed_once_with_proof(self):
        policy,_=self.update(evaluation_context=True)
        with self.assertRaises(PolicyBlocked):self.dispatch(policy)
        approval,created=self.approve();self.assertTrue(created)
        repeated,created=self.approve();self.assertFalse(created);self.assertEqual(approval['id'],repeated['id'])
        result=self.dispatch(policy);self.assertEqual(result['approval_id'],approval['id'])
        with raw_connect(self.config) as connection:
            row=connection.execute('SELECT decision_json,policy_json FROM pilotage_policy.authorizations').fetchone()
            used=connection.execute('SELECT used_by FROM pilotage_policy.approvals').fetchone()[0]
        self.assertEqual(used,'e'*32)
        self.assertFalse(json.loads(row[0])['decision']['evidence']['probe_success'])
        self.assertEqual(json.loads(row[1])['sha256'],policy['sha256'])
        with self.assertRaises(PolicyBlocked):self.dispatch(policy,run_id='f'*32)
        public=json.dumps(self.store.history())
        self.assertNotIn(self.session['token'],public);self.assertNotIn(PASSWORD,public)

    def test_parallel_dispatch_has_one_authorization_and_one_reservation(self):
        policy,_=self.update();calls=[]
        def dispatch(run_id):
            try:self.dispatch(policy,run_id=run_id,reserve=lambda:calls.append(run_id));return 'authorized'
            except PolicyBlocked:return 'blocked'
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(dispatch,('e'*32,'f'*32)))
        self.assertEqual(sorted(results),['authorized','blocked']);self.assertEqual(len(calls),1)
        self.assertEqual(self.store.history()['total'],2)

    def test_revoked_or_changed_approver_cannot_authorize_evaluation_action(self):
        policy,_=self.update(evaluation_context=True);self.approve()
        self.auth.update_user(self.user['id'],active=False,now=BASE+4)
        with self.assertRaisesRegex(PolicyBlocked,'evaluation_approval_required'):self.dispatch(policy)
        with raw_connect(self.config) as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM pilotage_policy.authorizations').fetchone()[0],0)

    def test_suspension_or_policy_revision_change_invalidates_prepared_execution(self):
        policy,_=self.update(mode='automatic',confirm_automatic=True)
        self.assertIsNotNone(self.store.reserve_worker(document(),now=BASE+3))
        self.assertIsNone(self.store.reserve_worker(document(),now=BASE+4))
        self.update(revision=2,mode='automatic',confirm_automatic=True,suspended=True,now=BASE+5)
        calls=[]
        with self.assertRaises(PolicyBlocked):self.dispatch(policy,automatic=True,reserve=lambda:calls.append(1),now=BASE+6)
        self.assertEqual(calls,[])

    def test_audit_failure_rolls_back_policy_change_in_the_same_transaction(self):
        with raw_connect(self.config) as connection:
            connection.execute("CREATE FUNCTION pilotage_policy.fail_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fixture failure'; END $$")
            connection.execute('CREATE TRIGGER fail_audit BEFORE INSERT ON pilotage_policy.events FOR EACH ROW EXECUTE FUNCTION pilotage_policy.fail_audit()')
        with self.assertRaises(PolicyStorageError):self.update()
        self.assertEqual(self.store.view(now=BASE+3)['policy']['revision'],1)
        self.assertEqual(self.store.history()['total'],0)


if __name__=='__main__':unittest.main()
