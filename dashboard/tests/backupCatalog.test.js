import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { catalogCurrent, catalogPath, validateCatalog } from '../src/backupCatalog.js';
import { validateDocument } from '../src/operations.js';

const now = Date.parse('2026-10-09T12:00:00Z');
const document = () => ({
  status: 'available', mode: 'read_only', scope: 'moodle', evaluated_at: '2026-10-09T12:00:00Z',
  reason: null, total: 2, listed_total: 2, truncated: false, limit: 10, offset: 0, has_more: false,
  latest_success_id: 'a'.repeat(64), latest_success_presence: 'present',
  collection: { status: 'up', observed_at: '2026-10-09T12:00:00Z', age_seconds: 0, max_age_seconds: 900 },
  verification: { repository_integrity: 'not_checked', restoration: 'not_checked' },
  items: [{ id: 'a'.repeat(64), archived_at: '2026-10-09T10:05:00Z', recovery_at: '2026-10-09T10:00:00Z',
    hostname: 'moodleprod', success_recorded: true },
  { id: 'b'.repeat(64), archived_at: '2026-10-08T10:05:00Z', recovery_at: null,
    hostname: 'moodleprod', success_recorded: false }],
});

test('dates, confirmations and pagination are distinct from restoration proof', () => {
  const value = validateDocument('backup_catalog', document());
  assert.equal(catalogCurrent(value, now), true);
  assert.equal(value.items[1].recovery_at, null);
  assert.equal(value.verification.restoration, 'not_checked');
  assert.equal(catalogPath('pilotage', 20), '/api/v1/backups/catalog?scope=pilotage&limit=10&offset=20');
  for (const args of [['all', 0], ['moodle', -1], ['moodle', 201]]) assert.throws(() => catalogPath(...args));
});

test('an interrupted or stale collection cannot keep old points visible', () => {
  const value = document();
  assert.equal(catalogCurrent(value, now+61000), false);
  assert.equal(catalogCurrent(value, now-31000), false);
  value.collection.age_seconds = 890;
  assert.equal(catalogCurrent(value, now+11000), false);
  value.status = 'unavailable';
  assert.equal(catalogCurrent(value, now), false);
});

test('corrupt dates, duplicated points, leaked fields and unsupported claims are rejected', () => {
  for (const change of [
    (v) => { v.items[1].recovery_at = '2026-10-08T09:00:00Z'; },
    (v) => { v.items[1].id = v.items[0].id; },
    (v) => { v.items[0].archived_at = '2026-10-10T10:00:00Z'; },
    (v) => { v.items[0].archived_at = '2026-02-30T10:00:00Z'; },
    (v) => { v.items[0].recovery_at = '2026-10-09T10:06:00Z'; },
    (v) => { v.items[0].paths = ['/secret']; },
    (v) => { v.items[0].success_recorded = false; },
    (v) => { v.items.reverse(); },
    (v) => { v.verification.restoration = 'guaranteed'; },
    (v) => { v.total = NaN; },
    (v) => { v.has_more = true; },
    (v) => { v.collection.age_seconds = 200; },
  ]) {
    const value = document();
    change(value);
    assert.throws(() => validateCatalog(value));
  }
});

test('both scopes cross the GET only proxy without permitting a restore route', async () => {
  const source = await readFile(new URL('../nginx.conf.template', import.meta.url), 'utf8');
  const match = source.match(/location ~ "([^"]+)" \{([\s\S]*?)\n        \}/);
  assert.match(match[2], /limit_except GET/);
  const path = new RegExp(match[1]);
  assert.equal(path.test('/api/v1/backups/catalog'), true);
  for (const value of ['/api/v1/backups/catalog/restore', '/api/v1/backups/pilotage/catalog',
    '/api/v1/backups/delete', '/api/v1/backups/catalogue']) assert.equal(path.test(value), false);
});

test('the panel exposes complete identifiers while keeping unknown dates and failures explicit', async (t) => {
  const { createServer } = await import('vite');
  const React = await import('react');
  const { renderToStaticMarkup } = await import('react-dom/server');
  const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  t.after(() => server.close());
  const { CatalogArea } = await server.ssrLoadModule('/src/BackupCatalog.jsx');
  const render = (value, instant = now) => renderToStaticMarkup(React.createElement(CatalogArea,
    { reading: { data: value, pending: false }, scope: 'moodle', offset: 0, now: instant,
      onScope: () => {}, onPage: () => {} }));
  const value = document();
  const normal = render(value);
  assert.match(normal, /Catalogue récent|Archivé le|Instant des données|Non enregistré/);
  assert.match(normal, new RegExp('a'.repeat(64)));
  assert.match(normal, /Sauvegarde confirmée/);
  assert.doesNotMatch(normal, /Restaurer|Supprimer|\/var\/lib|secret/);
  const stale = render(value, now+61000);
  assert.match(stale, /catalogue est ancien/);
  assert.doesNotMatch(stale, new RegExp('a'.repeat(64)));
  const failed = { ...value, status: 'unavailable', reason: 'repository_timeout', items: [] };
  assert.match(render(failed), /dépassé le délai/);
  const absent = { ...value, latest_success_presence: 'absent', latest_success_id: 'c'.repeat(64),
    items: value.items.map((p) => ({ ...p, success_recorded: false, recovery_at: null })) };
  assert.match(render(absent), /absent du dépôt/);
});
