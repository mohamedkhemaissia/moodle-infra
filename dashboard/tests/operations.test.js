import test from 'node:test';
import assert from 'node:assert/strict';
import { validateDocument, freshResult, analysisReady, decisionCurrent, recoveryProven, requestPath, RESOURCES } from '../src/operations.js';

const now = Date.parse('2026-10-07T20:57:30Z');
const iso = (x = now) => new Date(x).toISOString();
const analysis = (kind) => ({ status: 'available', experimental: true, evaluated_at: iso(), input_evaluated_at: iso(),
  collection: { linux: { status: 'up', scrape_age_seconds: 5 } },
  ...(kind === 'forecast' ? { forecasts: Object.fromEntries(['5','10','15'].map((h) => [h, {
    target_evaluated_at: iso(now + Number(h) * 60000),
    metrics: Object.fromEntries(RESOURCES.map(([key]) => [key, { value: 0, method: 'persistence' }])) }])) }
    : { anomaly: { score: 0.53, threshold: 0.65, statistical_anomaly: false, operational_rules: { alert: false } } }) });

test('Prévisions : zéro reste valide, les horizons ou méthodes invalides sont refusés', () => {
  assert.equal(validateDocument('forecast', analysis('forecast')).forecasts['5'].metrics.cpu_usage_percent.value, 0);
  for (const issue of ['missing', 'method', 'nan', 'overflow']) {
    const data = analysis('forecast');
    if (issue === 'missing') delete data.forecasts['15'];
    if (issue === 'method') data.forecasts['5'].metrics.cpu_usage_percent.method = 'unknown';
    if (issue === 'nan') data.forecasts['5'].metrics.cpu_usage_percent.value = NaN;
    if (issue === 'overflow') data.forecasts['5'].metrics.cpu_usage_percent.value = 101;
    assert.throws(() => validateDocument('forecast', data));
  }
});
test('Une analyse indisponible conserve son motif et aucun score n’est inventé', () => {
  const data = { status: 'unavailable', experimental: true, evaluated_at: iso(), input_evaluated_at: iso(),
    reason: 'insufficient_contiguous_history', anomaly: null };
  assert.equal(validateDocument('anomaly', data).anomaly, null);
  assert.equal(analysisReady(data, now), false);
});
test('Une analyse exige un historique et une collecte récents', () => {
  const value = analysis('anomaly');
  assert.equal(analysisReady(value, now), true);
  assert.equal(analysisReady(value, now + 61000), false);
  value.collection.linux.status = 'stale'; assert.equal(analysisReady(value, now), false);
  value.collection.linux.status = 'up'; value.input_evaluated_at = iso(now - 61000);
  assert.equal(analysisReady(value, now), false);
});
test('Une erreur de lecture, une date future ou un résultat ancien masque les données', () => {
  const result = { data: { evaluated_at: iso() }, receivedAt: now, error: null };
  assert.ok(freshResult(result, now));
  assert.equal(freshResult({ ...result, error: 'HTTP 503' }, now), null);
  assert.equal(freshResult(result, now + 61000), null);
  assert.equal(freshResult({ ...result, receivedAt: NaN }, now), null);
  assert.equal(freshResult({ ...result, data: { evaluated_at: iso(now + 31000) } }, now), null);
});
test('Les identifiants et états mal formés des incidents et journaux sont refusés', () => {
  const doc = { items: [{ id: 'a'.repeat(32), incident_id: 6, status: 'completed', started_at: now / 1000 }],
    total: 1, offset: 0, has_more: false };
  assert.equal(validateDocument('runs', doc).items[0].incident_id, 6);
  for (const value of [null, { ...doc, items: [{}] }, { ...doc, total: -1 }, { ...doc, items: [{ ...doc.items[0], status: 'healthy' }] }]) {
    assert.throws(() => validateDocument('runs', value));
  }
  assert.throws(() => validateDocument('incidents', { ...doc, items: [{ id: 6, status: 'open', title: 'Signal', severity: {} }] }));
});
test('Une recommandation expirée ou un instantané inutilisable ne paraît pas exécutable', () => {
  const value = { snapshot_usable: true, decision: { status: 'recommendation', input_evaluated_at: iso(now - 30000), valid_until: iso(now + 60000) } };
  assert.equal(decisionCurrent(value, now), true);
  assert.equal(decisionCurrent(value, now + 60000), false);
  assert.equal(decisionCurrent({ ...value, snapshot_usable: false }, now), false);
  value.decision.status = 'closed'; assert.equal(decisionCurrent(value, now), false);
});
test('Runner terminé ne suffit pas à annoncer une récupération', () => {
  assert.equal(recoveryProven({ status: 'completed', runner_rc: 0, report: { recovered: false, action_dispatched: false } }), false);
  assert.equal(recoveryProven({ status: 'blocked', runner_rc: 0 }), false);
  assert.equal(recoveryProven({ status: 'completed', report: { recovered: true, action_dispatched: true,
    reason: 'https_and_new_probe_confirmed' } }), true);
});
test('Seules les lectures prévues et les sélections valides construisent une URL', () => {
  assert.equal(requestPath('incidents', { status: 'resolved', offset: 10 }), '/api/v1/incidents?limit=10&offset=10&status=resolved');
  assert.equal(requestPath('remediations', { incidentId: 6 }), '/api/v1/remediations?limit=10&offset=0&incident_id=6');
  for (const [kind, options] of [['execute', {}], ['incidents', { status: 'all' }], ['decision', { incidentId: -1 }],
    ['remediations', { runId: '../execute' }], ['incidents', { offset: 100001 }]]) assert.throws(() => requestPath(kind, options));
});
