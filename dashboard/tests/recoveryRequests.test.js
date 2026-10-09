import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { validateDocument, freshResult } from '../src/operations.js';
import { canManageRecovery, approvalCurrent, requestKey, requestPath, validateRecoveryRequest, validateRecoveryMutation } from '../src/recoveryRequests.js';

const now = Date.parse('2026-10-09T12:00:00Z');
const row = () => ({ id:'a'.repeat(32), scope:'pilotage', snapshot_id:'b'.repeat(64), target:'isolated_test',
  reason:'Verifier cette copie isolee', status:'pending', revision:1, requested_by:{id:1,username:'hamma'},
  created_at:'2026-10-09T11:59:00Z', updated_at:'2026-10-09T11:59:00Z',
  selection:{scope:'pilotage',catalog_observed_at:'2026-10-09T11:59:00Z', point:{id:'b'.repeat(64),
    archived_at:'2026-10-09T11:00:00Z', recovery_at:null, hostname:'moodleprod', success_recorded:false}},
  decision:null, execution:{enabled:false,status:'not_dispatched'} });
const document = () => ({status:'available',mode:'approval_only',execution_enabled:false,evaluated_at:'2026-10-09T12:00:00Z',
  items:[row()],total:1,limit:10,offset:0,has_more:false});

test('a valid request has fixed isolated target, full reference and no execution claim', () => {
  assert.equal(validateDocument('recovery_requests', document()).items[0].target, 'isolated_test');
  for (const change of [(r)=>r.target='production', (r)=>r.execution.enabled=true, (r)=>r.execution.status='completed',
    (r)=>r.snapshot_id='b'.repeat(12), (r)=>r.selection.scope='moodle', (r)=>r.decision={}, (r)=>r.revision=0,
    (r)=>r.selection.point.recovery_at='2026-10-09T10:00:00Z']) {
    const value=row(); change(value); assert.throws(()=>validateRecoveryRequest(value));
  }
});

test('an approval expires independently of request age and never means a passed restore', () => {
  const value=row(); value.status='approved'; value.revision=2; value.updated_at='2026-10-09T12:00:00Z';
  value.decision={by:{id:2,username:'kais'},reason:'Valider le test isole',at:value.updated_at,
    valid_until:'2026-10-09T12:15:00Z',approval_valid:true};
  assert.equal(validateRecoveryRequest(value),value);
  assert.equal(approvalCurrent(value,now),true); assert.equal(approvalCurrent(value,now+900000),false);
  assert.equal(approvalCurrent(value,now-1),false);
  assert.equal(value.execution.status,'not_dispatched');
  assert.equal(validateRecoveryMutation({...document(),request:value}).request,value);
  assert.throws(()=>validateRecoveryMutation({...document(),request:value,execution_enabled:true}));
});

test('reader disabled authentication malformed pagination and stale responses hide mutations', () => {
  assert.equal(canManageRecovery({enabled:true,user:{role:'admin',active:true}}),true);
  for (const auth of [null,{enabled:false,user:{role:'admin',active:true}}, {enabled:true,user:{role:'reader',active:true}},
    {enabled:true,user:{role:'admin',active:false}}]) assert.equal(canManageRecovery(auth),false);
  const value=document(); assert.equal(freshResult({data:value,receivedAt:now},now+61000),null);
  value.has_more=true; assert.throws(()=>validateDocument('recovery_requests',value));
  assert.equal(requestKey({getRandomValues: (values)=>values.fill(17)}),'11'.repeat(16));
  assert.throws(()=>requestKey({})); assert.throws(()=>requestPath('../production'));
  assert.equal(requestPath('a'.repeat(32)),'/api/v1/recovery/requests/'+'a'.repeat(32));
});

test('the exact proxy paths accept only request and approval verbs', async () => {
  const source=await readFile(new URL('../nginx.conf.template',import.meta.url),'utf8');
  assert.match(source,/location = \/api\/v1\/recovery\/requests \{\s+limit_except GET POST/);
  const blocks=[...source.matchAll(/location ~ "([^"]*recovery[^\"]+)" \{([\s\S]*?)\n        \}/g)];
  const detail=blocks.find(([,pattern])=>pattern.endsWith('{32}$'));
  const decisions=blocks.find(([,pattern])=>pattern.includes('approve|reject|cancel'));
  assert.match(detail[2],/limit_except GET/); assert.match(decisions[2],/limit_except POST/);
  const path=new RegExp(decisions[1]);
  assert.equal(path.test(requestPath('a'.repeat(32))+'/approve'),true);
  for (const action of ['execute','restore','delete','approve/extra']) assert.equal(path.test(requestPath('a'.repeat(32))+'/'+action),false);
});

test('request detail displays audit while reader and cancelled request have no decision buttons', async (t) => {
  const {createServer}=await import('vite'); const React=await import('react');
  const {renderToStaticMarkup}=await import('react-dom/server');
  const server=await createServer({server:{middlewareMode:true,hmr:false},appType:'custom'}); t.after(()=>server.close());
  const {RequestDetail,RequestsArea}=await server.ssrLoadModule('/src/RecoveryRequests.jsx');
  const value={request:row(),events:[{id:1,action:'created',by:{id:1,username:'hamma'},at:'2026-10-09T11:59:00Z',revision:1,evidence:{}}]};
  const render=(admin)=>renderToStaticMarkup(React.createElement(RequestDetail,{value,admin,now,busy:false,onDecision:()=>{},onClose:()=>{}}));
  const admin=render(true); assert.match(admin,/Historique des décisions|Je confirme la référence/);
  assert.match(admin,/Approuver/); assert.match(admin,new RegExp('b'.repeat(64)));
  const reader=render(false); assert.doesNotMatch(reader,/Approuver|Refuser|Annuler la demande/); assert.match(reader,/Aucun test n’a été lancé/);
  const failed=renderToStaticMarkup(React.createElement(RequestsArea,{reading:{data:null,error:'Journal indisponible'},offset:0,onPage:()=>{},onOpen:()=>{}}));
  assert.match(failed,/Journal indisponible/); assert.doesNotMatch(failed,/Consulter|Approuver/);
});
