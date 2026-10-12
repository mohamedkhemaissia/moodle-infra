import test from 'node:test';
import assert from 'node:assert/strict';
import { validateHistory, historyPath, makeHistoryReport } from '../src/decisionHistory.js';
import { reportCsv, reportHtml } from '../src/reports.js';
import { readFile } from 'node:fs/promises';

const at = '2026-10-11T12:00:00Z';
const entry = { id: 2, incident_id: 1, status: 'recommendation', persisted: true,
  first_observed_at: at, last_observed_at: at, observations: 1,
  decision: { incident_id: 1, status: 'recommendation', signal_key: 'cpu_high', context: 'model-' + 'a'.repeat(64),
    explanation: 'Signal confirmé', input_evaluated_at: at, policy_version: '1.1.0', policy_sha256: 'b'.repeat(64),
    decision_id: 'c'.repeat(64), execution_allowed: false, reason_codes: ['confirmed_recent_signal'], evidence: { cpu: 90 },
    recommended_action: { title: 'Diagnostic CPU', executable: false, steps: ['Lire les mesures'] } },
  policy: { version: '1.1.0', sha256: 'b'.repeat(64) }, collector: { status: 'running' } };
const fixture = () => ({ items: [structuredClone(entry)], has_more: false, next_before_id: null, execution_allowed: false,
  coverage: { status: 'running', started_at: at, last_input_at: at, last_evaluated_at: at, backfilled: false } });

test('Le journal refuse les preuves incohérentes et conserve la pagination et les filtres', () => {
  const value = fixture();
  assert.equal(validateHistory(value), value);
  assert.equal(historyPath({ beforeId: 2, incidentId: '1', status: 'closed' }),
    '/api/v1/decisions/history?limit=50&status=closed&before_id=2&incident_id=1');
  for (const options of [{ incidentId: '1&x=y' }, { beforeId: 0 }, { status: 'failed' }]) assert.throws(() => historyPath(options));
  assert.throws(() => validateHistory(value, { beforeId: 2 }));
  assert.throws(() => validateHistory(value, { status: 'closed' }));
  assert.throws(() => validateHistory(value, { incidentId: '2' }));
  for (const mutate of [
    (v) => { v.items[0].decision.execution_allowed = true; },
    (v) => { v.items[0].policy.sha256 = 'e'.repeat(64); },
    (v) => { v.items[0].observations = 0; },
    (v) => { v.items[0].last_observed_at = '2000-01-01T00:00:00Z'; },
    (v) => { v.items.push(v.items[0]); },
    (v) => { v.coverage.backfilled = true; },
    (v) => { v.has_more = true; v.next_before_id = 2; },
  ]) { const broken = fixture(); mutate(broken); assert.throws(() => validateHistory(broken)); }
});

test('Les exports bornent leur périmètre et neutralisent HTML et formules CSV', () => {
  const value = fixture();
  value.items[0].decision.explanation = '=HYPERLINK("bad")';
  value.items[0].decision.recommended_action.title = '<script>alert(1)</script>';
  const report = makeHistoryReport(value, {}, 0, Date.parse(at));
  const csv = reportCsv(report, Date.parse(at));
  assert.match(csv, /'=HYPERLINK/);
  assert.match(csv, /""cpu"":90/);
  const html = reportHtml(report, Date.parse(at));
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /Seule la page sélectionnée/);
  assert.match(html, /Aucun historique reconstruit/);
  assert.match(html, /ne permet aucune exécution/);
});

test('Le panneau réel montre les preuves historiques sans bouton d’exécution', async (t) => {
  const { createServer } = await import('vite');
  const React = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  t.after(() => server.close());
  const { HistoryView } = await server.ssrLoadModule('/src/DecisionHistory.jsx');
  const value = fixture();
  value.coverage.status = 'stale';
  const html = renderToStaticMarkup(React.createElement(HistoryView, { value, selectedId: 2, onSelect() {} }));
  assert.match(html, /Collecte obsolète/);
  assert.match(html, /Mesures à la première évaluation/);
  assert.match(html, /CPU utilisé/);
  assert.doesNotMatch(html, /<pre|policy_sha256|&quot;cpu&quot;/);
  assert.match(html, /1\.1\.0/);
  assert.match(html, /ne permet aucune exécution/);
  assert.doesNotMatch(html, />Exécuter|>Approuver|>Corriger/);
  value.coverage.status = 'running';
  const live = renderToStaticMarkup(React.createElement(HistoryView, { value, selectedId: 2, onSelect() {}, now: Date.parse(at) + 2000 }));
  assert.match(live, /Observateur actif/);
  const aged = renderToStaticMarkup(React.createElement(HistoryView, { value, selectedId: 2, onSelect() {}, now: Date.parse(at) + 91000 }));
  assert.match(aged, /Collecte obsolète/);
  assert.match(aged, /Mesures à la première évaluation/);
});

test('Le proxy expose les deux lectures du journal et refuse les routes d’action', async () => {
  const source = await readFile(new URL('../nginx.conf.template', import.meta.url), 'utf8');
  const match = source.match(/location ~ "([^"]+)" \{([\s\S]*?)\n        \}/);
  const route = new RegExp(match[1]);
  assert.ok(route.test('/api/v1/decisions/history'));
  assert.ok(route.test('/api/v1/decisions/history/2'));
  for (const path of ['history/0', 'history/2/execute', 'history/execute']) assert.ok(!route.test('/api/v1/decisions/' + path));
  assert.match(match[2], /limit_except GET \{ deny all; \}/);
});
