import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { siteOperationActive, validateSiteMutation, validateSiteOperations } from '../src/siteOperations.js';

const operation = {
  id: 'a'.repeat(32), client_key: 'b'.repeat(32), kind: 'backup', status: 'queued',
  requested_by: { id: 1, username: 'admin' }, created_at: '2026-10-15T10:00:00Z',
  updated_at: '2026-10-15T10:00:00Z', started_at: null, finished_at: null,
};

test('validates the minimal full-site view', () => {
  const value = validateSiteOperations({ status: 'available', mode: 'site_full',
    evaluated_at: '2026-10-15T10:00:00Z', latest_success_at: '2026-10-15T09:00:00Z',
    points: [{ id: '1'.repeat(64), created_at: '2026-10-15T08:59:00Z' }], operations: [operation] });
  assert.equal(value.points.length, 1);
  assert.equal(siteOperationActive(value.operations[0]), true);
});

test('rejects technical or malformed data', () => {
  assert.throws(() => validateSiteOperations({ status: 'available', mode: 'site_full',
    latest_success_at: null, points: [{ id: 'short', created_at: 'today' }], operations: [] }));
  assert.throws(() => validateSiteMutation({ status: 'available', mode: 'site_full', created: true,
    operation: { ...operation, command: 'docker restart' , id: 'bad' } }));
});

test('validates queued mutations', () => {
  const value = validateSiteMutation({ status: 'available', mode: 'site_full', created: true, operation });
  assert.equal(value.operation.kind, 'backup');
});

test('the proxy exposes only the minimal site operation routes', () => {
  const nginx = readFileSync(new URL('../nginx.conf.template', import.meta.url), 'utf8');
  assert.match(nginx, /location = \/api\/v1\/site-operations \{[\s\S]*?limit_except GET/);
  assert.match(nginx, /site-operations\/\(backups\|restores\)\$[\s\S]*?limit_except POST/);
  assert.doesNotMatch(nginx, /site-operations\/[^"\n]*download/);
});
