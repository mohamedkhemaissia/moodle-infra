import test from 'node:test';
import assert from 'node:assert/strict';
import { NAVIGATION, sectionFromHash } from '../src/navigation.js';
import { operationRequests } from '../src/operations.js';

test('deep links resolve each menu and unavailable accounts fall back to the overview', () => {
  for (const item of NAVIGATION) assert.equal(sectionFromHash('#' + item.id), item.id);
  for (const hash of ['', '#unknown', '#content', null]) assert.equal(sectionFromHash(hash), 'main');
  assert.equal(sectionFromHash('#accounts'), 'main');
  assert.equal(sectionFromHash('#accounts', true), 'accounts');
});

test('each operations view requests only its own information and preserves the selected incident', () => {
  const options = { status: 'open', offset: 10, incidentId: 6, diagnosticOffset: 20,
    remediationOffset: 30, run: { kind: 'diagnostics', id: 'a'.repeat(32) } };
  assert.deepEqual(operationRequests('analysis', options).map((r) => [r.key, r.path]),
    [['anomaly', '/api/v1/anomalies/summary']]);
  const incidents = operationRequests('incidents', options);
  assert.deepEqual(incidents.map((r) => r.key), ['summary', 'incidents', 'decision']);
  assert.equal(incidents[2].path, '/api/v1/decisions/6');
  const audits = operationRequests('audits', options);
  assert.deepEqual(audits.map((r) => r.key), ['diagnostics', 'remediations', 'run']);
  assert.equal(audits[0].path, '/api/v1/diagnostics?limit=10&offset=20&incident_id=6');
  assert.equal(audits[1].path, '/api/v1/remediations?limit=10&offset=30&incident_id=6');
  assert.equal(audits[2].path, '/api/v1/diagnostics/' + 'a'.repeat(32));
  assert.equal(operationRequests('incidents').length, 2);
  assert.equal(operationRequests('audits').length, 2);
  assert.throws(() => operationRequests('reports'));
});

test('each actual menu renders its own panel without the other views', async (t) => {
  const { createServer } = await import('vite');
  const React = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  t.after(() => server.close());
  const { SectionContent, default: App } = await server.ssrLoadModule('/src/App.jsx');
  const { AuthContext } = await server.ssrLoadModule('/src/Auth.jsx');
  const auth = { enabled: true, user: { id: 1, display_name: 'Opérateur', role: 'admin', active: true } };
  const ids = [...NAVIGATION.map((item) => item.id), 'accounts'];
  for (const section of ids) {
    const html = renderToStaticMarkup(React.createElement(AuthContext.Provider, { value: auth },
      React.createElement(SectionContent, { section, now: Date.now(), refreshToken: 0,
        metrics: { cpu_usage_percent: null, memory_usage_percent: null, disk_available_percent: null } })));
    const expected = section === 'main' ? 'overview' : section;
    assert.match(html, new RegExp(`<section[^>]*id="${expected}"`), section);
    for (const other of ids.filter((id) => id !== section)) {
      assert.doesNotMatch(html, new RegExp(`<section[^>]*id="${other === 'main' ? 'overview' : other}"`), section + '/' + other);
    }
  }
  const previousWindow = globalThis.window;
  try {
    for (const [section, title] of [['reports', 'Rapports'], ['containers', 'Conteneurs'], ['main', 'Vue d’ensemble']]) {
      globalThis.window = { location: { hash: '#' + section } };
      const html = renderToStaticMarkup(React.createElement(AuthContext.Provider, { value: auth }, React.createElement(App)));
      assert.match(html, new RegExp(`<h1>${title}</h1>`));
      assert.match(html, new RegExp(`href="#${section}" aria-current="page"`));
      if (section !== 'resources') assert.doesNotMatch(html, /<section[^>]*id="resources"/);
      if (section !== 'reports') assert.doesNotMatch(html, /<section[^>]*id="reports"/);
    }
  } finally {
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
  }
});
