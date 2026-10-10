import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import httpx

from app.main import create_app
from pilotage.auth import AuthDenied
from pilotage.operating_policy import PolicyConflict,PolicyStorageError
from pilotage.worker_auth import token_fingerprint
from test_auth_api import AccessFixture,ORIGIN,PASSWORD,TOKEN,csrf_token


class PolicyFixture:
    def __init__(self):self.calls=[];self.error=None
    def check(self):pass
    def invoke(self,name):
        self.calls.append(name)
        if self.error:raise self.error
    def view(self):self.invoke('view');return {'status':'available','policy':{'revision':1}}
    def history(self,**kwargs):self.invoke('history');return {'items':[],'total':0,'limit':kwargs['limit'],'offset':kwargs['offset'],'has_more':False}
    def update(self,principal,**kwargs):
        self.invoke('update');self.actor=principal;self.parameters=kwargs
        return {'revision':2},True
    def approve(self,principal,document,**kwargs):
        self.invoke('approve');self.actor=principal;self.document=document;self.parameters=kwargs
        return {'id':'a'*32,'valid_until':'2026-10-10T13:30:00Z'},True


class PolicyAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth=AccessFixture();self.store=PolicyFixture()
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        worker=Path(temp.name)/'worker';worker.write_text(token_fingerprint('c'*64));worker.chmod(0o600)
        self.app=create_app(auth_enabled=True,auth_store=self.auth,auth_origins=[ORIGIN],auth_secure_cookie=False,
            auth_worker_hash_file=str(worker),policies_enabled=True,policy_store=self.store,decisions_enabled=True,incidents_dir=temp.name)
        self.life=self.app.router.lifespan_context(self.app);await self.life.__aenter__()
        self.addAsyncCleanup(self.life.__aexit__,None,None,None)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app,client=('127.0.0.1',1234)),base_url=ORIGIN)
        self.addAsyncCleanup(self.client.aclose)
        self.headers={'Origin':ORIGIN,'X-CSRF-Token':csrf_token(TOKEN)}
        self.body={'revision':1,'mode':'assisted','suspended':False,'evaluation_context':True,'reason':'Proteger les apprenants pendant ce test'}
    async def login(self):await self.client.post('/api/v1/auth/login',headers={'Origin':ORIGIN},json={'username':'hamma','password':PASSWORD})

    async def test_anonymous_reader_worker_and_csrf_fail_before_writes(self):
        self.assertEqual((await self.client.get('/api/v1/policies')).status_code,401)
        self.auth.user['role']='reader';await self.login()
        self.assertEqual((await self.client.get('/api/v1/policies')).status_code,200)
        self.assertEqual((await self.client.post('/api/v1/policies',headers=self.headers,json=self.body)).status_code,403)
        self.auth.user['role']='admin'
        self.assertEqual((await self.client.post('/api/v1/policies',headers={'Origin':ORIGIN},json=self.body)).status_code,403)
        worker={'Authorization':'Bearer '+'c'*64}
        self.assertEqual((await self.client.get('/api/v1/policies',headers=worker)).status_code,200)
        self.assertEqual((await self.client.get('/api/v1/policies/events',headers=worker)).status_code,403)
        self.assertEqual((await self.client.post('/api/v1/policies',headers={**worker,**self.headers},json=self.body)).status_code,403)
        self.assertNotIn('update',self.store.calls)

    async def test_admin_change_preserves_principal_and_strict_fields(self):
        await self.login()
        response=await self.client.post('/api/v1/policies',headers=self.headers,json=self.body)
        self.assertEqual(response.status_code,200);self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertEqual(self.store.actor['user']['id'],1);self.assertEqual(self.store.parameters['evaluation_context'],True)
        for fields in ({'revision':True},{'suspended':'true'},{'mode':'root'},{'actor_id':999},{'reason':'short'}):
            self.assertEqual((await self.client.post('/api/v1/policies',headers=self.headers,json={**self.body,**fields})).status_code,422)
        self.assertEqual(self.store.calls.count('update'),1)

    async def test_approval_uses_server_decision_without_accepting_supplied_evidence(self):
        await self.login()
        body={'revision':1,'incident_id':5,'reason':'Autoriser la reprise Moodle pendant le test','client_key':'d'*32}
        with patch('app.operating_policy.summarize',return_value={'decision':{'incident_id':5}}) as summary:
            response=await self.client.post('/api/v1/policies/approvals',headers=self.headers,json=body)
        self.assertEqual(response.status_code,201);self.assertFalse(response.json()['action_dispatched'])
        self.assertEqual(summary.call_args.args[2],5);self.assertEqual(self.store.document['decision']['incident_id'],5)
        self.assertEqual((await self.client.post('/api/v1/policies/approvals',headers=self.headers,json={**body,'decision_id':'e'*64})).status_code,422)

    async def test_store_conflicts_revocation_and_failures_are_explicit(self):
        await self.login()
        for error,code in ((PolicyConflict('fixture'),409),(PolicyStorageError('fixture'),503),(AuthDenied('fixture'),403)):
            self.store.error=error
            self.assertEqual((await self.client.post('/api/v1/policies',headers=self.headers,json=self.body)).status_code,code)

    async def test_disabled_policies_and_invalid_pagination_do_not_become_empty_success(self):
        await self.login()
        for path in ('/api/v1/policies/events?limit=51','/api/v1/policies/events?offset=-1'):
            self.assertEqual((await self.client.get(path)).status_code,422)
        self.app.state.operating_policy=None
        self.assertEqual((await self.client.get('/api/v1/policies')).status_code,503)

    def test_missing_authentication_or_decision_dependencies_refuse_activation(self):
        for kwargs in ({'auth_enabled':False},{'decisions_enabled':False},{'incidents_dir':''}):
            with self.assertRaises(ValueError):create_app(policies_enabled=True,**kwargs)

    async def test_remediation_summary_reports_current_mode_and_fails_closed_on_policy_loss(self):
        await self.login()
        class Runs:
            def summary(self):return {'mode':'manual_guarded','automatic_execution_enabled':False,'semantics':{}}
        self.app.state.remediations=Runs()
        def automatic():return {'policy':{'mode':'automatic','suspended':False,'evaluation_context':True},'worker':{'status':'idle'}}
        with patch.object(self.store,'view',side_effect=automatic):
            response=await self.client.get('/api/v1/remediations/summary')
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json()['automatic_execution_enabled'])
        self.assertTrue(response.json()['operating_policy']['evaluation_context'])
        self.assertEqual(response.json()['mode'],'policy_guarded')
        with patch.object(self.store,'view',side_effect=PolicyStorageError('fixture')):
            self.assertEqual((await self.client.get('/api/v1/remediations/summary')).status_code,503)
