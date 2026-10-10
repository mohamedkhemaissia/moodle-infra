import assert from 'node:assert/strict';
import test from 'node:test';
import { PAGE_SIZE, csvCell, loadReport, makeReport, reportCsv, reportHtml, reportPath } from '../src/reports.js';

const now = Date.parse('2026-10-10T12:00:00Z');
const iso = '2026-10-10T11:50:00Z';
const incident = (id = 13) => ({ id, title: 'Historique insuffisant', signal_key: 'analysis_history',
  category: 'data_quality', severity: 'warning', status: 'open', latest_state: 'triggered',
  first_observed_at: iso, opened_at: iso, last_observed_at: iso, resolved_at: null });
const page = (items, total = items.length, offset = 0) => ({ items, total, offset, limit: PAGE_SIZE,
  has_more: offset + items.length < total });
const restore = () => ({ id: 'a'.repeat(32), scope: 'moodle', snapshot_id: 'b'.repeat(64), target: 'isolated_test',
  reason: 'Verifier cette copie isolee', status: 'approved', revision: 2, requested_by: { id: 1, username: 'hamma' },
  created_at: iso, updated_at: iso, selection: { scope: 'moodle', catalog_observed_at: iso,
    point: { id: 'b'.repeat(64), archived_at: '2026-10-10T11:00:00Z', recovery_at: null, hostname: 'moodleprod', success_recorded: false } },
  decision: { by: { id: 1, username: 'hamma' }, reason: 'Valider ce test isole', at: iso,
    valid_until: '2026-10-10T12:05:00Z', approval_valid: true },
  execution: { enabled: true, status: 'failed', id: 'c'.repeat(32), queued_at: iso,
    started_at: '2026-10-10T11:51:00Z', finished_at: '2026-10-10T11:55:00Z', reason_code: 'test_output_too_large',
    report: { status: 'failed', runner_rc: 0, runner_status: 'successful', production_changed: false,
      tables_verified: 0, cleanup: { containers_removed: true, networks_removed: true, temporary_files_removed: true } } } });

test('un export conserve les identifiants, dates UTC, filtre et pages exclues', () => {
  const report = makeReport('incidents', page([incident(13), incident(12)], 52, 50), { offset: 50, status: 'open', retrievedAt: now });
  assert.equal(report.rows[0][0], 13);
  assert.equal(report.rows[0][5], 'Ouvert');
  assert.equal(report.rows[0][10], '');
  assert.equal(report.filter, 'Ouverts');
  const csv = reportCsv(report, now + 300000);
  assert.match(csv, /^\ufeff/);
  assert.match(csv, /"Page";"Total à la lecture";"Lignes exportées";"Autres pages"/);
  assert.match(csv, /"2";"52";"2";"Oui"/);
  assert.match(csv, /2026-10-10T12:00:00.000Z/);
  assert.match(csv, /2026-10-10T12:05:00.000Z/);
  const html = reportHtml(report, now);
  assert.match(html, /Seule la page sélectionnée/);
  assert.match(html, /Non renseigné/);
  assert.match(html, /Entrée 51/);
});

test('les cellules CSV neutralisent les formules meme precedees d espaces', () => {
  for (const value of ['=1+1', '+SUM(A1:A2)', '-1+2', '@SUM(1)', '\t=1+1', '\r\n@SUM(1)', '\uFEFF +1']) {
    assert.ok(csvCell(value).startsWith('"\''));
  }
  assert.equal(csvCell('Cours "Quiz";A\nB'), '"Cours ""Quiz"";A\nB"');
  assert.equal(csvCell('Texte simple'), '"Texte simple"');
  assert.equal(csvCell(0), '"0"');
});

test('le HTML echappe les contenus et seuls les champs publics choisis sont exportes', () => {
  const value = incident(); value.title = '<img src=x onerror="alert(1)"> & test';
  value.token = 'secret-not-for-export'; value.evidence = { password: 'secret-not-for-export' };
  const report = makeReport('incidents', page([value]), { retrievedAt: now });
  const html = reportHtml(report, now);
  assert.match(html, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt; &amp; test/);
  assert.doesNotMatch(html, /<img|<script|secret-not-for-export|<link|https?:\/\//);
  assert.match(html, /Content-Security-Policy/);
  assert.doesNotMatch(reportCsv(report, now), /secret-not-for-export/);
});

test('un test echoue reste echoue meme si Runner a termine avec le code zero', () => {
  const document = { ...page([restore()]), status: 'available', mode: 'isolated_test', execution_enabled: true, evaluated_at: iso };
  const report = makeReport('recovery', document, { retrievedAt: now });
  assert.equal(report.rows[0][2], 'b'.repeat(64));
  assert.equal(report.rows[0][9], 'Test échoué');
  assert.equal(report.rows[0][14], 'Non');
  assert.equal(report.rows[0][15], '');
  assert.equal(report.rows[0][16], 0);
  assert.equal(report.rows[0][18], 'Oui');
  const html = reportHtml(report, now);
  assert.match(html, /Test échoué/); assert.doesNotMatch(html, /Restauration vérifiée|restore_validated/);
});

test('les journaux d execution gardent les codes negatifs et ne prouvent pas un retablissement', () => {
  const value = { id: 'd'.repeat(32), incident_id: 6, action_id: 'start_php_stopped', status: 'completed',
    started_at: now / 1000, finished_at: null, runner_rc: -9, runner_status: 'successful', private_path: '/secret' };
  const report = makeReport('remediations', page([value]), { retrievedAt: now });
  assert.equal(report.rows[0][3], 'Exécution terminée'); assert.equal(report.rows[0][5], ''); assert.equal(report.rows[0][7], -9);
  assert.match(reportHtml(report, now), /ne prouve pas à elle seule le rétablissement/);
  assert.doesNotMatch(reportCsv(report, now), /secret/);
});

test('les incoherences de pagination, doublons, filtres et champs mal formes sont refuses', () => {
  for (const value of [page([incident()], 2), { ...page([incident()]), offset: 50 },
    { ...page([incident()]), limit: 10 }, { ...page([incident()]), has_more: true },
    page([incident(), incident()]), page([{ ...incident(), latest_state: {} }]),
    page([{ ...incident(), resolved_at: 'invalide' }])]) {
    assert.throws(() => makeReport('incidents', value, { retrievedAt: now }));
  }
  assert.throws(() => makeReport('incidents', page([incident()]), { status: 'resolved', retrievedAt: now }));
  for (const [kind, options] of [['delete', {}], ['diagnostics', { status: 'open' }], ['incidents', { offset: 1 }],
    ['recovery', { offset: 10050 }], ['incidents', { offset: -50 }]]) assert.throws(() => reportPath(kind, options));
});

test('une page vide est un rapport vide sans resultat invente', () => {
  const report = makeReport('incidents', page([]), { retrievedAt: now });
  assert.equal(report.rows.length, 0); assert.equal(report.total, 0);
  assert.match(reportHtml(report, now), /Aucune entrée sur cette page/);
  assert.doesNotMatch(reportHtml(report, now), /Entrée 1/);
  assert.equal(reportCsv(report, now).split('\r\n').length, 2);
});

test('les lectures passent par la session existante et un refus HTTP ne produit aucun export', async () => {
  let called = 0;
  const report = await loadReport('diagnostics', {}, async (path, options) => {
    called++;
    assert.equal(path, '/api/v1/diagnostics?limit=50&offset=0');
    assert.equal(options.method, 'GET'); assert.equal(options.credentials, 'same-origin'); assert.equal(options.cache, 'no-store');
    return { ok: true, json: async () => page([]) };
  });
  assert.equal(called, 1); assert.equal(report.rows.length, 0);
  await assert.rejects(() => loadReport('incidents', {}, async () => ({ ok: false, status: 403,
    json: () => { throw new Error('Une erreur ne doit pas etre lue comme des donnees.'); } })), /HTTP 403/);
});

test('le rendu React montre le perimetre et bloque les exports sans lecture valide', async (t) => {
  const { createServer } = await import('vite'); const React = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  t.after(() => server.close());
  const { ReportView } = await server.ssrLoadModule('/src/Reports.jsx');
  const render = (props) => renderToStaticMarkup(React.createElement(ReportView, { onDownload() {}, onPage() {}, onRefresh() {}, ...props }));
  const failed = render({ report: null, pending: false, error: 'Accès refusé' });
  assert.match(failed, /Accès refusé/); assert.match(failed, /disabled=""[^>]*>Exporter la page CSV/);
  const report = makeReport('incidents', page([incident()]), { retrievedAt: now });
  const html = render({ report, pending: false, error: null });
  assert.match(html, /Historique insuffisant/); assert.match(html, /données de cette page/);
  assert.match(html, /Exporter la page CSV/); assert.match(html, /Rapport HTML/);
});
