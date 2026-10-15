import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { NAVIGATION, NAVIGATION_GROUPS } from '../src/navigation.js';
import { makeReport, reportCsv } from '../src/reports.js';
import { dashboardFixtures } from './fixtures.js';

const now = Date.parse('2026-10-11T21:30:00Z');

test('groups keep every previous route, reports, policies, backups and accounts reachable', () => {
  const sections = [...NAVIGATION_GROUPS.flatMap((group) => group.sections), 'main', 'backups'];
  assert.equal(new Set(sections).size, sections.length);
  for (const item of NAVIGATION) assert.ok(sections.includes(item.id), item.id);
  for (const id of ['incidents', 'decision-history', 'audits', 'policies', 'reports', 'accounts', 'backups']) assert.ok(sections.includes(id));
});

test('responsive tables retain semantic headers and vertical labels for every data cell', async (t) => {
  const { createServer } = await import('vite');
  const React = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  t.after(() => server.close());
  const { CompactTable } = await server.ssrLoadModule('/src/ApplicationServices.jsx');
  const html = renderToStaticMarkup(React.createElement(CompactTable, { caption: 'Exemple accessible',
    rows: [{ id: 1 }], rowKey: (row) => row.id, columns: [
      { key: 'name', label: 'Nom', render: () => 'Moodle' }, { key: 'state', label: 'État', render: () => 'Inconnu' },
    ] }));
  assert.match(html, /<caption[^>]*>Exemple accessible/);
  assert.equal((html.match(/scope="col"/g) || []).length, 2);
  assert.match(html, /scope="row" data-label="Nom"/);
  assert.match(html, /<td data-label="État"/);
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
  assert.match(css, /@media \(max-width: 720px\)/);
  assert.match(css, /content: attr\(data-label\)/);
  assert.match(css, /table-layout: fixed/);
  assert.doesNotMatch(css, /min-width: (?:650|890)px/);
});

test('catalogue is consultation-only for administrators too, with no isolated-test action', async (t) => {
  const { createServer } = await import('vite');
  const React = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  t.after(() => server.close());
  const { CatalogArea } = await server.ssrLoadModule('/src/BackupCatalog.jsx');
  const { catalog } = dashboardFixtures(now);
  const html = renderToStaticMarkup(React.createElement(CatalogArea, { now, reading: { data: catalog }, scope: 'moodle', offset: 0,
    onScope() {}, onPage() {}, onRequest() { throw new Error('Le catalogue doit rester en consultation.'); } }));
  assert.doesNotMatch(html, /Test isolé|Demander un test|Restaurer|Supprimer/);
  assert.match(html, /Présent dans le dépôt|Sauvegarde confirmée/);
  assert.match(html, /Consulter/);
  assert.equal((html.match(/scope="col"/g) || []).length, 5);
});

test('tables stay compact while containers use visual cards', async (t) => {
  const { createServer } = await import('vite');
  const React = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  t.after(() => server.close());
  const f = dashboardFixtures(now);
  const { ApplicationTable } = await server.ssrLoadModule('/src/ApplicationServices.jsx');
  const { ContainerTable } = await server.ssrLoadModule('/src/Containers.jsx');
  const { IncidentTable } = await server.ssrLoadModule('/src/Operations.jsx');
  const { HistoryView } = await server.ssrLoadModule('/src/DecisionHistory.jsx');
  const { RequestsArea } = await server.ssrLoadModule('/src/RecoveryRequests.jsx');
  const { ReportView } = await server.ssrLoadModule('/src/Reports.jsx');
  const { PolicyView } = await server.ssrLoadModule('/src/Policies.jsx');
  const { EvidenceSummary, RunSummary } = await server.ssrLoadModule('/src/ReadableDetails.jsx');
  const report = makeReport('incidents', { ...f.incidents, limit: 50 }, { retrievedAt: now });
  const recoveryReport = makeReport('recovery', { ...f.recovery, limit: 50 }, { retrievedAt: now });
  const policy = { policy: { mode: 'observation', suspended: false, evaluation_context: false,
    revision: 1, updated_by: 'hamma', updated_at: f.at }, worker: { status: 'idle' }, eventTotal: 1,
    events: [{ id: 1, at: f.at, action: 'policy_updated', actor: 'hamma', revision: 1, details: { reason: 'Motif conservé dans le détail' } }] };
  const samples = [
    [ApplicationTable, { now, reading: { data: f.applications }, onSelect() {} }],
    [IncidentTable, { items: f.incidents.items, onSelect() {} }],
    [HistoryView, { now, value: f.history, onSelect() {} }],
    [RequestsArea, { now, reading: { data: f.recovery }, onOpen() {}, onPage() {}, offset: 0 }],
    [ReportView, { report }],
    [ReportView, { report: recoveryReport }],
    [PolicyView, { data: policy, admin: false }],
  ];
  for (const [component, props] of samples) {
    const html = renderToStaticMarkup(React.createElement(component, props));
    const headers = (html.match(/scope="col"/g) || []).length;
    assert.ok(headers > 0 && headers <= 5, component.name + ': ' + headers);
    assert.match(html, /data-label=/);
    assert.doesNotMatch(html, /<details[^>]* open(?:=|\s|>)/);
    const primary = html.replace(/<details\b[^>]*>[\s\S]*?<\/details>/g, '');
    assert.doesNotMatch(primary, /<pre/);
    assert.doesNotMatch(html, /<pre|href="\/api\/v1\//);
  }
  const containers = renderToStaticMarkup(React.createElement(ContainerTable, { now, reading: { data: f.containers } }));
  assert.match(containers, /container-card-grid/);
  assert.match(containers, /Apache|PHP-FPM|PostgreSQL/);
  assert.doesNotMatch(containers, /Comprendre les mesures|scope="col"/);
  const incidents = renderToStaticMarkup(React.createElement(ReportView, { report }));
  assert.match(incidents, /Gravité|Critique/);
  assert.match(incidents, /CPU élevé/);
  const recovery = renderToStaticMarkup(React.createElement(ReportView, { report: recoveryReport }));
  assert.match(recovery, /Création \(UTC\)/);
  const hiddenRow = [...recoveryReport.rows[0]];
  hiddenRow[1] = 'Pilotage PostgreSQL';
  const recoveryFiltered = renderToStaticMarkup(React.createElement(ReportView,
    { report: { ...recoveryReport, rows: [...recoveryReport.rows, hiddenRow] } }));
  assert.doesNotMatch(recoveryFiltered, /Pilotage PostgreSQL/);
  const evidence = renderToStaticMarkup(React.createElement(EvidenceSummary,
    { evidence: { metric: 'cpu_usage_percent', value: 0, probe_success: false, policy_sha256: 'sensitive-reference' } }));
  assert.match(evidence, /CPU utilisé/);
  assert.match(evidence, /0 %/);
  assert.match(evidence, /Échec/);
  assert.doesNotMatch(evidence, /sensitive-reference|policy_sha256|<pre/);
  const diagnostic = renderToStaticMarkup(React.createElement(RunSummary, { run: {
    action_id: 'diagnose_cpu_usage', started_at: now / 1000 - 10, finished_at: now / 1000,
    report: { mode: 'read_only', checks: { current_metrics: f.snapshot, service_states: { status: 'warning' } } },
  } }));
  assert.match(diagnostic, /Diagnostic CPU/);
  assert.match(diagnostic, /CPU utilisé/);
  assert.match(diagnostic, /À vérifier/);
  assert.doesNotMatch(diagnostic, /<pre|&quot;metrics&quot;/);
  assert.match(reportCsv(report, now), /cpu_high/); // Secondary columns remain in exports.
});

test('dashboard shows Moodle backups only and preserves admin-only recovery requests', async (t) => {
  const { createServer } = await import('vite');
  const React = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  t.after(() => server.close());
  const { AuthContext } = await server.ssrLoadModule('/src/Auth.jsx');
  const { default: Backups } = await server.ssrLoadModule('/src/Backups.jsx');
  const { default: RecoveryRequests } = await server.ssrLoadModule('/src/RecoveryRequests.jsx');
  const markup = (component, props, role = 'admin') => renderToStaticMarkup(React.createElement(AuthContext.Provider,
    { value: { enabled: true, user: { role, active: true } } }, React.createElement(component, props)));
  const backup = markup(Backups, { now });
  assert.doesNotMatch(backup, /Sauvegardes du pilotage|PostgreSQL du pilotage/);
  assert.match(backup, /Sauvegarde complète du site/);
  assert.match(backup, /<details[^>]*><summary>Sauvegarde d’un cours<\/summary>/);
  assert.doesNotMatch(backup, /Identifiant complet|SHA-256|\/var\/lib/);
  const props = { now, onSelectPoint() {}, onCloseSelection() {} };
  assert.match(markup(RecoveryRequests, props), /Préparer une demande/);
  assert.doesNotMatch(markup(RecoveryRequests, props, 'reader'), /Préparer une demande|Approuver|Lancer le test/);
});
