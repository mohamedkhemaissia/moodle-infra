import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { NAVIGATION, NAVIGATION_GROUPS } from '../src/navigation.js';
import { makeReport, reportCsv } from '../src/reports.js';
import { RESOURCES, validateDocument } from '../src/operations.js';
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

test('services, containers, incidents, decisions, forecasts, policies and reports have at most five columns', async (t) => {
  const { createServer } = await import('vite');
  const React = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  t.after(() => server.close());
  const f = dashboardFixtures(now);
  const { ApplicationTable } = await server.ssrLoadModule('/src/ApplicationServices.jsx');
  const { ContainerTable } = await server.ssrLoadModule('/src/Containers.jsx');
  const { IncidentTable, Forecasts } = await server.ssrLoadModule('/src/Operations.jsx');
  const { HistoryView } = await server.ssrLoadModule('/src/DecisionHistory.jsx');
  const { RequestsArea } = await server.ssrLoadModule('/src/RecoveryRequests.jsx');
  const { ReportView } = await server.ssrLoadModule('/src/Reports.jsx');
  const { PolicyView } = await server.ssrLoadModule('/src/Policies.jsx');
  const report = makeReport('incidents', { ...f.incidents, limit: 50 }, { retrievedAt: now });
  const recoveryReport = makeReport('recovery', { ...f.recovery, limit: 50 }, { retrievedAt: now });
  const forecast = validateDocument('forecast', { status: 'available', experimental: true,
    evaluated_at: f.at, input_evaluated_at: f.at, collection: { linux: { status: 'up', scrape_age_seconds: 2 } },
    forecasts: Object.fromEntries(['5', '10', '15'].map((horizon) => [horizon, {
      target_evaluated_at: new Date(now + Number(horizon) * 60000).toISOString(),
      metrics: Object.fromEntries(RESOURCES.map(([key]) => [key, { value: 10, method: 'persistence' }])),
    }])) });
  const policy = { policy: { mode: 'observation', suspended: false, evaluation_context: false,
    revision: 1, updated_by: 'hamma', updated_at: f.at }, worker: { status: 'idle' }, eventTotal: 1,
    events: [{ id: 1, at: f.at, action: 'policy_updated', actor: 'hamma', revision: 1, details: { reason: 'Motif conservé dans le détail' } }] };
  const samples = [
    [ApplicationTable, { now, reading: { data: f.applications }, onSelect() {} }],
    [ContainerTable, { now, reading: { data: f.containers } }],
    [IncidentTable, { items: f.incidents.items, onSelect() {} }],
    [HistoryView, { now, value: f.history, onSelect() {} }],
    [RequestsArea, { now, reading: { data: f.recovery }, onOpen() {}, onPage() {}, offset: 0 }],
    [ReportView, { report }],
    [ReportView, { report: recoveryReport }],
    [Forecasts, { result: { data: forecast }, now }],
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
  }
  const incidents = renderToStaticMarkup(React.createElement(ReportView, { report }));
  assert.match(incidents, /Gravité|Critique/);
  assert.match(incidents, /CPU élevé/);
  const recovery = renderToStaticMarkup(React.createElement(ReportView, { report: recoveryReport }));
  assert.match(recovery, /Création \(UTC\)/);
  assert.match(reportCsv(report, now), /cpu_high/); // Secondary columns remain in exports.
});

test('backup technical and restore information remains secondary, with admin-only requests preserved', async (t) => {
  const { createServer } = await import('vite');
  const React = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  t.after(() => server.close());
  const { AuthContext } = await server.ssrLoadModule('/src/Auth.jsx');
  const { default: Backups, BackupArea } = await server.ssrLoadModule('/src/Backups.jsx');
  const { default: RecoveryRequests } = await server.ssrLoadModule('/src/RecoveryRequests.jsx');
  const markup = (component, props, role = 'admin') => renderToStaticMarkup(React.createElement(AuthContext.Provider,
    { value: { enabled: true, user: { role, active: true } } }, React.createElement(component, props)));
  const backup = markup(Backups, { now });
  assert.match(backup, /<details[^>]*><summary>Sauvegardes du pilotage<\/summary>/);
  assert.match(backup, /<details[^>]*><summary>Gestion des restaurations<\/summary>/);
  const { pilotage } = dashboardFixtures(now);
  const secondary = markup(BackupArea, { now, pilotage: true, reading: { data: pilotage } });
  assert.match(secondary, /PostgreSQL du pilotage|31 tables/);
  assert.match(secondary, /<details[^>]*><summary>Preuves de restauration PostgreSQL<\/summary>/);
  assert.match(secondary, /Dernier test de restauration isolée/);
  const props = { now, onSelectPoint() {}, onCloseSelection() {} };
  assert.match(markup(RecoveryRequests, props), /Préparer une demande/);
  assert.doesNotMatch(markup(RecoveryRequests, props, 'reader'), /Préparer une demande|Approuver|Lancer le test/);
});
