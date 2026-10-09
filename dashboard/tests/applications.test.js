import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { SERVICE_KEYS, validateApplications, applicationView } from '../src/applicationMetrics.js';
import { validateDocument } from '../src/operations.js';

const now = Date.parse('2026-10-09T09:00:00Z');
function document() {
  return { status: 'available', mode: 'read_only', source: 'prometheus',
    evaluated_at: '2026-10-09T09:00:00Z', max_scrape_age_seconds: 60,
    services: Object.fromEntries(Object.entries(SERVICE_KEYS).map(([name, keys]) => [name, {
      status: 'available', collection: { status: 'up', scrape_age_seconds: 2 },
      service_reachable: true, metrics: Object.fromEntries(keys.map((key) => [key, 0])), unavailable_metrics: [],
    }])) };
}

test('valid zero values are shown without treating them as missing', () => {
  const d = validateDocument('applications', document());
  assert.equal(applicationView(d, 'php_fpm', now).metrics.active_processes, 0);
});

test('old API documents and aging scrape samples are masked independently', () => {
  const d = document();
  for (const moment of [now + 61_000, now - 31_000]) {
    assert.equal(applicationView(d, 'php_fpm', moment).reachable, null);
    assert.equal(applicationView(d, 'php_fpm', moment).metrics.active_processes, null);
  }
  d.services.php_fpm.collection.scrape_age_seconds = 50;
  assert.equal(applicationView(d, 'php_fpm', now + 11_000).metrics.active_processes, null);
});

test('impossible availability, unsafe values and unknown schema are rejected', () => {
  for (const mutate of [
    (d) => { d.services.php_fpm.service_reachable = false; },
    (d) => { d.services.php_fpm.collection.status = 'down'; },
    (d) => { d.services.php_fpm.metrics.listen_queue = -1; },
    (d) => { d.services.php_fpm.metrics.listen_queue = Infinity; },
    (d) => { delete d.services.php_fpm.metrics.listen_queue; },
    (d) => { d.services.php_fpm.unavailable_metrics = ['listen_queue']; },
    (d) => { d.services.php_fpm.status = 'unavailable'; },
    (d) => { d.mode = 'automatic'; },
  ]) {
    const d = document(); mutate(d); assert.throws(() => validateApplications(d));
  }
});

test('a failed exporter remains distinct from a current failed service connection', () => {
  const d = document();
  d.status = 'partial';
  const s = d.services.php_fpm;
  s.status = 'unavailable'; s.service_reachable = false;
  s.unavailable_metrics = [...SERVICE_KEYS.php_fpm];
  for (const key of SERVICE_KEYS.php_fpm) s.metrics[key] = null;
  assert.equal(applicationView(validateApplications(d), 'php_fpm', now).reachable, false);
  s.service_reachable = null; s.collection.status = 'down';
  assert.equal(applicationView(validateApplications(d), 'php_fpm', now).collection, 'down');
  assert.equal(applicationView(d, 'php_fpm', now + 61_000).collection, 'unknown');
});

test('the actual proxy forwards the exact read route and refuses invented action routes', async () => {
  const template = await readFile(new URL('../nginx.conf.template', import.meta.url), 'utf8');
  const match = template.match(/location ~ "([^"]+)" \{([\s\S]*?)\n        \}/);
  assert.ok(match);
  const route = new RegExp(match[1]);
  assert.equal(route.test('/api/v1/metrics/applications'), true);
  assert.equal(route.test('/api/v1/metrics/summary'), true);
  assert.equal(route.test('/api/v1/metrics/applications/restart'), false);
  assert.match(match[2], /limit_except GET \{ deny all; \}/);
});

test('the services panel renders counts and hides outdated or failed readings', async (t) => {
  const { createServer } = await import('vite');
  const React = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  t.after(() => server.close());
  const { ApplicationArea } = await server.ssrLoadModule('/src/ApplicationServices.jsx');
  const markup = (reading, moment = now) => renderToStaticMarkup(React.createElement(ApplicationArea,
    { name: 'php_fpm', reading, now: moment }));
  const current = markup({ data: document(), pending: false });
  assert.match(current, /PHP-FPM/);
  assert.match(current, /Accès confirmé/);
  assert.match(current, /Requêtes en attente/);
  assert.doesNotMatch(current, /<button|mot de passe|\/opt\//);
  assert.doesNotMatch(markup({ data: document(), pending: false }, now + 61_000), /Accès confirmé/);
  assert.match(markup({ data: null, pending: false, error: 'Lecture indisponible' }), /Lecture indisponible/);
});
