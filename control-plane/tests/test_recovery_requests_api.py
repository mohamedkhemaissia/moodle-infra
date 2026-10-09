import unittest
from pathlib import Path
import tempfile
import httpx

from app.main import create_app
from pilotage.auth import AuthDenied
from pilotage.recovery import RecoveryConflict, RecoveryNotFound, RecoveryStorageError
from pilotage.worker_auth import token_fingerprint
from test_auth_api import AccessFixture, ORIGIN, PASSWORD, TOKEN, csrf_token


class JournalFixture:
    def __init__(self): self.calls=[]; self.error=None; self.created=True
    def check(self): pass
    def perform(self, name):
        self.calls.append(name)
        if self.error: raise self.error
    def listing(self, **kwargs):
        self.perform('listing'); return dict(items=[], total=0, limit=kwargs['limit'], offset=kwargs['offset'], has_more=False)
    def detail(self, identifier):
        self.perform('detail'); return {'request':{'id':identifier}, 'events':[]}
    def create(self, principal, **kwargs):
        self.perform('create'); self.actor=principal
        return {'id':'a'*32}, self.created
    def transition(self, principal, identifier, **kwargs):
        self.perform(kwargs['action']); self.transition_input=kwargs
        return {'id':identifier}


class RecoveryRequestAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth=AccessFixture(); self.store=JournalFixture()
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        worker=Path(temp.name)/'worker'; worker.write_text(token_fingerprint('c'*64)); worker.chmod(0o600)
        self.app=create_app(auth_enabled=True, auth_store=self.auth, auth_origins=[ORIGIN], auth_secure_cookie=False,
            auth_worker_hash_file=str(worker), backup_catalog_file='/fixture/catalog',
            recovery_requests_enabled=True, recovery_store=self.store)
        self.life=self.app.router.lifespan_context(self.app); await self.life.__aenter__()
        self.addAsyncCleanup(self.life.__aexit__,None,None,None)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=('127.0.0.1',1234)),base_url=ORIGIN)
        self.addAsyncCleanup(self.client.aclose)
        self.path='/api/v1/recovery/requests'
        self.body=dict(scope='moodle',snapshot_id='b'*64,reason='Verifier la copie isolee',client_key='d'*32)
        self.headers={'Origin':ORIGIN,'X-CSRF-Token':csrf_token(TOKEN)}

    async def login(self):
        return await self.client.post('/api/v1/auth/login',headers={'Origin':ORIGIN},json={'username':'hamma','password':PASSWORD})

    async def test_anonymous_worker_reader_and_csrf_boundaries_precede_mutation(self):
        self.assertEqual((await self.client.get(self.path)).status_code,401)
        self.assertEqual((await self.client.get(self.path,headers={'Authorization':'Bearer '+'c'*64})).status_code,403)
        self.auth.user['role']='reader'; await self.login()
        self.assertEqual((await self.client.get(self.path)).status_code,200)
        for path in (self.path, self.path+'/'+'a'*32+'/approve'):
            self.assertEqual((await self.client.post(path,headers=self.headers,json=self.body)).status_code,403)
        self.auth.user['role']='admin'
        for headers in ({}, {'Origin':ORIGIN}, {**self.headers,'Origin':'https://foreign.invalid'}):
            self.assertEqual((await self.client.post(self.path,headers=headers,json=self.body)).status_code,403)
        self.assertEqual(self.store.calls,['listing'])

    async def test_admin_creation_idempotent_status_and_audit_principal_are_explicit(self):
        await self.login()
        created=await self.client.post(self.path,headers=self.headers,json=self.body)
        self.assertEqual(created.status_code,201); self.assertFalse(created.json()['execution_enabled'])
        self.assertEqual(created.json()['mode'],'approval_only'); self.assertEqual(created.headers['cache-control'],'no-store')
        self.assertEqual(self.store.actor['user']['id'],1)
        self.store.created=False
        self.assertEqual((await self.client.post(self.path,headers=self.headers,json=self.body)).status_code,200)

    async def test_input_cannot_supply_production_target_actor_paths_or_incomplete_identifiers(self):
        await self.login()
        for change in ({'target':'production'}, {'actor_id':999}, {'path':'/opt/moodle'}, {'snapshot_id':'b'*12},
                       {'client_key':'../invalid'}, {'scope':'all'}, {'reason':'short'}):
            self.assertEqual((await self.client.post(self.path,headers=self.headers,json={**self.body,**change})).status_code,422)
        self.assertEqual(self.store.calls,[])
        for query in ('limit=51','offset=-1','status=completed'):
            self.assertEqual((await self.client.get(self.path+'?'+query)).status_code,422)

    async def test_decisions_require_revision_and_full_confirmation_and_do_not_execute(self):
        await self.login(); root=self.path+'/'+'a'*32
        body={'revision':1,'reason':'Valider ce test isole'}
        self.assertEqual((await self.client.post(root+'/approve',headers=self.headers,json=body)).status_code,422)
        self.assertEqual((await self.client.post(root+'/approve',headers=self.headers,json={**body,'revision':True,'confirmed_snapshot_id':'b'*64})).status_code,422)
        approved=await self.client.post(root+'/approve',headers=self.headers,json={**body,'confirmed_snapshot_id':'b'*64})
        self.assertEqual(approved.status_code,200); self.assertFalse(approved.json()['execution_enabled'])
        self.assertEqual(self.store.transition_input['confirmed_snapshot_id'],'b'*64)
        for action in ('reject','cancel'):
            self.assertEqual((await self.client.post(root+'/'+action,headers=self.headers,json=body)).status_code,200)
        self.assertEqual((await self.client.post(root+'/execute',headers=self.headers,json={})).status_code,404)
        self.assertEqual((await self.client.request('DELETE',root,headers=self.headers,json={})).status_code,415)

    async def test_conflict_revoked_session_missing_record_and_storage_failure_have_safe_status(self):
        await self.login()
        for error, expected in ((RecoveryConflict('Catalogue ancien.'),409), (AuthDenied('Session revoquee.'),403),
                               (RecoveryNotFound('Demande inconnue.'),404), (RecoveryStorageError('postgres secret'),503)):
            self.store.error=error
            response=await self.client.post(self.path,headers=self.headers,json=self.body)
            self.assertEqual(response.status_code,expected)
            self.assertNotIn('postgres secret',response.text)

    async def test_readers_can_read_history_but_disabled_auth_never_opens_the_workflow(self):
        self.auth.user['role']='reader'; await self.login()
        result=await self.client.get(self.path+'/'+'a'*32)
        self.assertEqual(result.status_code,200); self.assertEqual(result.json()['events'],[])
        with self.assertRaises(ValueError): create_app(auth_enabled=False,recovery_requests_enabled=True,backup_catalog_file='/fixture')
        app=create_app(auth_enabled=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN) as client:
                self.assertEqual((await client.get(self.path)).status_code,403)


if __name__=='__main__': unittest.main()
