import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { validateDocument } from '../src/operations.js';
import { CONTAINER_KEYS, containerView, validateContainers } from '../src/containerMetrics.js';

const now = Date.parse('2026-10-10T20:00:00Z');
function document() {
  return { status: 'available', source: 'prometheus', mode: 'read_only',
    evaluated_at: '2026-10-10T20:00:00Z', max_scrape_age_seconds: 60, max_collection_age_seconds: 90,
    collection: { status: 'up', scrape_age_seconds: 2 }, collector: { status: 'up', age_seconds: 2 },
    containers: [{ project: 'moodle-pilote', service: 'php', state: 'running', health: null, instances: 1,
      stats_age_seconds: 2, metrics_status: 'available', metrics: { cpu_usage_percent: 0,
        memory_working_set_bytes: 1048576, memory_limit_bytes: 2097152, pids: 2 } }] };
}

test('zero CPU and CPU above 100 percent remain valid readings', () => {
  const d = validateDocument('containers', document());
  assert.equal(containerView(d, now).rows[0].metrics.cpu_usage_percent, 0);
  d.containers[0].metrics.cpu_usage_percent = 240;
  assert.equal(containerView(validateContainers(d), now).rows[0].metrics.cpu_usage_percent, 240);
});

test('old timer files cannot appear healthy because Node Exporter is still scraped', () => {
  const d = document(); d.collector.age_seconds = 80;
  const view = containerView(validateContainers(d), now + 11_000);
  assert.equal(view.fresh, false);
  assert.equal(view.rows[0].state, 'unknown');
  assert.ok(CONTAINER_KEYS.every((key) => view.rows[0].metrics[key] === null));
});

test('old statistics mask resources while a current inventory keeps its state', () => {
  const d = document(); d.containers[0].stats_age_seconds = 80;
  const view = containerView(validateContainers(d), now + 11_000);
  assert.equal(view.rows[0].state, 'running');
  assert.equal(view.rows[0].metrics.memory_working_set_bytes, null);
  for (const moment of [now + 61_000, now - 31_000]) {
    assert.equal(containerView(d, moment).rows[0].state, 'unknown');
  }
});

test('impossible readings and duplicated service identities are rejected', () => {
  for (const mutate of [
    (d) => { d.collection.status = 'down'; }, (d) => { d.collector.status = 'stale'; },
    (d) => { d.containers[0].state = 'not_running'; }, (d) => { d.containers[0].instances = 2; },
    (d) => { d.containers[0].metrics.cpu_usage_percent = Infinity; },
    (d) => { d.containers[0].metrics.memory_working_set_bytes = -1; },
    (d) => { delete d.containers[0].metrics.pids; },
    (d) => { d.containers.push({ ...d.containers[0] }); },
    (d) => { d.containers[0].stats_age_seconds = 91; },
  ]) {
    const d = document(); mutate(d); assert.throws(() => validateContainers(d));
  }
});

test('a configured absent service remains different from an unknown collection', () => {
  const d = document(); const row = d.containers[0];
  row.instances = 0; row.state = 'missing'; row.metrics_status = 'unavailable';
  row.stats_age_seconds = null; row.metrics = Object.fromEntries(CONTAINER_KEYS.map((key) => [key, null]));
  assert.equal(containerView(validateContainers(d), now).rows[0].state, 'missing');
  d.collector.status = 'down'; d.status = 'unavailable'; row.state = 'unknown'; row.instances = null;
  assert.equal(containerView(validateContainers(d), now).rows[0].state, 'unknown');
});

test('the actual proxy exposes the exact GET route and refuses fabricated actions', async () => {
  const source = await readFile(new URL('../nginx.conf.template', import.meta.url), 'utf8');
  const match = source.match(/location ~ "([^"]+)" \{([\s\S]*?)\n        \}/);
  const route = new RegExp(match[1]);
  assert.ok(route.test('/api/v1/metrics/containers'));
  assert.ok(!route.test('/api/v1/metrics/containers/restart'));
  assert.match(match[2], /limit_except GET \{ deny all; \}/);
});

test('the actual panel shows resources and hides old data without action controls', async (t) => {
  const { createServer } = await import('vite');
  const React = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  t.after(() => server.close());
  const { ContainerTable } = await server.ssrLoadModule('/src/Containers.jsx');
  const markup = (moment) => renderToStaticMarkup(React.createElement(ContainerTable,
    { reading: { data: document(), pending: false }, now: moment }));
  const current = markup(now);
  assert.match(current, /Démarré/); assert.match(current, /1 Mio/);
  assert.match(current, /moodle-pilote/);
  assert.doesNotMatch(current, /<button|\/var\/run|\/opt\//);
  const old = markup(now + 61_000);
  assert.match(old, /État inconnu/); assert.doesNotMatch(old, /1 Mio|Démarré/);
});
