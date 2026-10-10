import assert from 'node:assert/strict';
import test from 'node:test';
import { MODE_NAMES,approvalPayload,policyPayload,validatePolicy,loadPolicies } from '../src/policies.js';
import { readFile } from 'node:fs/promises';

const policy = () => ({ revision: 2, mode: 'assisted', suspended: false, evaluation_context: true,
  sha256: 'a'.repeat(64), updated_at: '2026-10-10T13:00:00Z', updated_by: 'hamma', reason: 'Protéger cette évaluation pédagogique' });
const form = () => ({ mode: 'automatic', suspended: false, evaluation_context: true, reason: 'Activer ce contrôle pendant une évaluation', confirm_automatic: true });

test('trois modes avec revision attendue et confirmation explicite de l automatique', () => {
  assert.deepEqual(Object.keys(MODE_NAMES), ['observation', 'assisted', 'automatic']);
  assert.equal(policyPayload(policy(), form()).revision, 2);
  assert.throws(() => policyPayload(policy(), { ...form(), confirm_automatic: false }));
  assert.throws(() => policyPayload(policy(), { ...form(), suspended: 'true' }));
  assert.throws(() => policyPayload(policy(), { ...form(), reason: 'short' }));
});
test('une approbation lie incident politique et cle sans inventer de preuve technique', () => {
  const value = approvalPayload(policy(), '5', 'Approuver le démarrage de PHP confirmé arrêté', 'b'.repeat(32));
  assert.equal(value.incident_id, 5); assert.equal(value.revision, 2);
  assert.equal(value.decision_id, undefined); assert.equal(value.valid_until, undefined);
  for (const change of [{ suspended: true }, { mode: 'observation' }, { evaluation_context: false }]) {
    assert.throws(() => approvalPayload({ ...policy(), ...change }, '5', 'Approuver cette intervention', 'b'.repeat(32)));
  }
  assert.throws(() => approvalPayload(policy(), '5e1', 'Approuver cette intervention', 'b'.repeat(32)));
});
test('les politiques incoherentes sont masquees et ne deviennent pas une configuration par defaut', () => {
  for (const change of [{ revision: true }, { mode: 'execute-anything' }, { evaluation_context: 1 }, { sha256: 'short' }, { updated_at: 'invalid' }]) {
    assert.throws(() => validatePolicy({ ...policy(), ...change }));
  }
});
test('les lectures conservent la session et la suspension affichee vient du serveur', async () => {
  const calls = [];
  const fetcher = async (path, options) => {
    calls.push({ path, options });
    return { ok: true, status: 200, json: async () => path.includes('/events')
      ? { status: 'available', items: [], total: 0 }
      : { status: 'available', policy: { ...policy(), suspended: true }, worker: { status: 'stale' } } };
  };
  const value = await loadPolicies(undefined, fetcher);
  assert.equal(value.policy.suspended, true); assert.equal(value.worker.status, 'stale');
  assert.equal(calls.length, 2); assert.ok(calls.every(({ options }) => options.credentials === 'same-origin' && options.method === 'GET'));
});
test('un refus de session ou un historique invalide ne produit pas une vue disponible', async () => {
  await assert.rejects(() => loadPolicies(undefined, async () => ({ status: 403, ok: false, json: async () => ({ detail: 'Refusé' }) })), /Refusé/);
  await assert.rejects(() => loadPolicies(undefined, async (path) => ({ status: 200, ok: true, json: async () => path.includes('/events')
    ? { status: 'available', total: 1, items: [{ id: 1, action: 'run-shell' }] }
    : { status: 'available', policy: policy(), worker: { status: 'idle' } } })), /invalide/);
});
test('un lecteur voit le mode et l historique sans controles de modification', async () => {
  const { createServer } = await import('vite');
  const React = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  try {
    const { PolicyView } = await server.ssrLoadModule('/src/Policies.jsx');
    const data = { policy: policy(), worker: { status: 'idle' }, events: [], eventTotal: 0 };
    const reader = renderToStaticMarkup(React.createElement(PolicyView, { data, admin: false }));
    assert.match(reader, /Assisté/); assert.match(reader, /approbation requise/);
    assert.doesNotMatch(reader, /Enregistrer la politique|Approuver le démarrage/);
    const admin = renderToStaticMarkup(React.createElement(PolicyView, { data, admin: true, form: form(), approval: { incident: '', reason: '' }, busy: true }));
    assert.match(admin, /disabled/); assert.match(admin, /J’autorise le worker/);
    assert.match(admin, /90 secondes/);
  } finally { await server.close(); }
});

test('le proxy ne transmet que les chemins et methodes du parcours de politique', async () => {
  const config = await readFile(new URL('../nginx.conf.template', import.meta.url), 'utf8');
  const exact = [...config.matchAll(/location = (\/api\/v1\/policies(?:\/events|\/approvals)?) \{([\s\S]*?)\n        \}/g)];
  assert.equal(exact.length, 3);
  const permitted = new Map(exact.map((match) => [match[1], match[2].match(/limit_except ([A-Z ]+) \{ deny all; \}/)[1].split(' ')]));
  assert.deepEqual(permitted.get('/api/v1/policies'), ['GET', 'POST']);
  assert.deepEqual(permitted.get('/api/v1/policies/events'), ['GET']);
  assert.deepEqual(permitted.get('/api/v1/policies/approvals'), ['POST']);
  assert.ok(exact.every((match) => match[2].includes('proxy_pass ${API_ORIGIN};')));
  assert.ok(!permitted.has('/api/v1/policies/execute'));
  assert.match(config, /location \/api\/ \{ return 404; \}/);
});
