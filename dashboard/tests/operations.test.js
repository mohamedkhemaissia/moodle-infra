import test from 'node:test';
import assert from 'node:assert/strict';
import { validateDocument, freshResult, analysisReady, decisionCurrent, recoveryProven, requestPath, RESOURCES } from '../src/operations.js';

const now = Date.parse('2026-10-07T20:57:30Z');
const iso = (x = now) => new Date(x).toISOString();
const analysis = (kind) => ({ status: 'available', experimental: true, evaluated_at: iso(), input_evaluated_at: iso(),
  collection: { linux: { status: 'up', scrape_age_seconds: 5 } },
  current_metrics: { cpu_usage_percent: 20, memory_usage_percent: 40, disk_available_percent: 70 },
  ...(kind === 'forecast' ? { forecasts: Object.fromEntries(['5','10','15'].map((h) => [h, {
    target_evaluated_at: iso(now + Number(h) * 60000),
    metrics: Object.fromEntries(RESOURCES.map(([key]) => [key, { value: 0, method: 'persistence' }])) }])) }
    : { anomaly: { score: 0.53, threshold: 0.65, statistical_anomaly: false, operational_rules: { alert: false } } }) });

test('Prévisions : zéro reste valide, les horizons ou méthodes invalides sont refusés', () => {
  assert.equal(validateDocument('forecast', analysis('forecast')).forecasts['5'].metrics.cpu_usage_percent.value, 0);
  const trend = analysis('forecast');
  trend.forecasts['5'].metrics.cpu_usage_percent.method = 'linear_trend';
  assert.equal(validateDocument('forecast', trend).forecasts['5'].metrics.cpu_usage_percent.method, 'linear_trend');
  for (const issue of ['missing', 'method', 'nan', 'overflow']) {
    const data = analysis('forecast');
    if (issue === 'missing') delete data.forecasts['15'];
    if (issue === 'method') data.forecasts['5'].metrics.cpu_usage_percent.method = 'unknown';
    if (issue === 'nan') data.forecasts['5'].metrics.cpu_usage_percent.value = NaN;
    if (issue === 'overflow') data.forecasts['5'].metrics.cpu_usage_percent.value = 101;
    assert.throws(() => validateDocument('forecast', data));
  }
  const invalidCurrent = analysis('forecast');
  invalidCurrent.current_metrics.cpu_usage_percent = NaN;
  assert.throws(() => validateDocument('forecast', invalidCurrent));
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

test('L’écran IA présente les anomalies et prévisions indépendamment, sans inventer un état normal', async (t) => {
  const { createServer } = await import('vite');
  const React = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  t.after(() => server.close());
  const { AnalysisView, Anomalies } = await server.ssrLoadModule('/src/AIAnalysis.jsx');
  const render = (component, props) => renderToStaticMarkup(React.createElement(component, props));
  const data = { ...analysis('anomaly'), model_sha256: 'b'.repeat(64), model_created_at: iso(now - 86400000) };
  data.anomaly.score = 0;
  const normal = render(Anomalies, { result: { data }, now });
  assert.match(normal, /Activité habituelle/);
  assert.match(normal, /20 %|40 %|70 %/);
  assert.match(normal, /Aucune intervention nécessaire/);
  assert.match(normal, /0,000/);
  assert.match(normal, /Isolation Forest/);
  assert.doesNotMatch(normal, /Modèle et observations|Version · référence|<pre|b{12,64}|&quot;score&quot;/);
  const atypical = structuredClone(data);
  atypical.anomaly.score = 0.8;
  atypical.anomaly.statistical_anomaly = true;
  const warning = render(Anomalies, { result: { data: atypical }, now, onNavigate() {} });
  assert.match(warning, /Variation inhabituelle détectée/);
  assert.match(warning, /Examiner les incidents/);
  for (const result of [
    { data, stale: true }, { data: { ...data, input_evaluated_at: iso(now - 61000) } },
    { data: { ...data, anomaly: { ...data.anomaly, statistical_anomaly: true } } },
    { error: 'HTTP 503' }, { pending: true },
    { data: { ...data, status: 'unavailable', reason: 'insufficient_contiguous_history', required_history_seconds: 420 } },
  ]) {
    const html = render(Anomalies, { result, now });
    assert.match(html, /Analyse temporairement indisponible/);
    assert.doesNotMatch(html, /Activité habituelle|0,000|ai-state-banner usual/);
  }
  const forecast = { ...analysis('forecast'), model_sha256: 'c'.repeat(64) };
  forecast.forecasts['10'].metrics.cpu_usage_percent.method = 'ridge';
  const view = render(AnalysisView, { anomaly: { error: 'HTTP 503' }, forecast: { data: forecast }, now });
  assert.match(view, /service d’analyse est indisponible/);
  assert.match(view, /Prévisions disponibles/);
  assert.match(view, /Évolution attendue des ressources/);
  assert.match(view, /Maintenant/);
  assert.match(view, /0 %/);
  assert.match(view, /\+5 min|\+10 min|\+15 min/);
  assert.doesNotMatch(view, /Modèle, méthodes et horizons|Régression Ridge|Maintien de la dernière valeur|<pre|c{12,64}/);
  const reverse = render(AnalysisView, { anomaly: { data }, forecast: { error: 'HTTP 503' }, now });
  assert.match(reverse, /Activité habituelle/);
  assert.doesNotMatch(reverse, /Prévisions disponibles/);
});
