import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import {
  diagnosticActive,
  diagnosticClientKey,
  diagnosticRequestsPath,
  latestDiagnosticRequest,
  validateDiagnosticMutation,
  validateDiagnosticRequests,
} from '../src/diagnosticRequests.js';

const at = '2026-10-14T10:00:00Z';
const request = (status = 'queued', id = 'a'.repeat(32)) => ({
  id, incident_id: 7, status, requested_by: { id: 1, username: 'hamma' },
  created_at: at, updated_at: status === 'queued' ? at : '2026-10-14T10:00:10Z',
  started_at: status === 'queued' ? null : '2026-10-14T10:00:05Z',
  finished_at: ['queued', 'running'].includes(status) ? null : '2026-10-14T10:00:10Z',
  run_id: ['queued', 'running'].includes(status) ? null : id,
  reason_code: status === 'blocked' ? 'decision_not_current' : null,
  result_url: ['queued', 'running'].includes(status) ? null : '/api/v1/diagnostics/' + id,
});
const list = (items = [request()]) => ({ status: 'available', mode: 'read_only', evaluated_at: at,
  items, total: items.length, limit: 10, offset: 0, has_more: false });

test('la file de diagnostic conserve le périmètre, les états et le lien exact vers le résultat', () => {
  assert.equal(validateDiagnosticRequests(list(), 7).items[0].status, 'queued');
  assert.equal(validateDiagnosticRequests(list([request('running')]), 7).items[0].run_id, null);
  assert.equal(validateDiagnosticRequests(list([request('completed')]), 7).items[0].result_url,
    '/api/v1/diagnostics/' + 'a'.repeat(32));
  const workerFailure = { ...request('failed'), run_id: null, result_url: null, reason_code: 'diagnostic_worker_failed' };
  assert.equal(validateDiagnosticRequests(list([workerFailure]), 7).items[0].result_url, null);
  assert.equal(validateDiagnosticMutation({ status: 'available', mode: 'read_only', evaluated_at: at,
    request: request(), created: true }, 7).created, true);
  assert.equal(diagnosticActive(request()), true);
  assert.equal(diagnosticActive(request('completed')), false);
});

test('les réponses incohérentes ne deviennent ni demande ni résultat affichable', () => {
  const values = [
    { ...request(), incident_id: 8 },
    { ...request('completed'), result_url: '/api/v1/diagnostics/' + 'b'.repeat(32) },
    { ...request('completed'), finished_at: null },
    { ...request('failed'), run_id: null, result_url: null, reason_code: null },
    { ...request('running'), started_at: null },
    { ...request(), requested_by: { id: 1, username: '../admin' } },
  ];
  for (const value of values) assert.throws(() => validateDiagnosticRequests(list([value]), 7));
  assert.throws(() => validateDiagnosticRequests({ ...list(), has_more: true }, 7));
  assert.throws(() => validateDiagnosticMutation({ status: 'available', mode: 'read_only', evaluated_at: at,
    request: request(), created: 'oui' }, 7));
});

test('le chemin et la clé idempotente restent strictement bornés', () => {
  assert.equal(diagnosticRequestsPath(7), '/api/v1/diagnostic-requests?incident_id=7&limit=10&offset=0');
  assert.throws(() => diagnosticRequestsPath(0));
  assert.throws(() => diagnosticRequestsPath(7, 10001));
  const key = diagnosticClientKey({ getRandomValues: (bytes) => bytes.fill(15) });
  assert.equal(key, '0f'.repeat(16));
});

test('la demande la plus récente est choisie sans dépendre de l’ordre reçu', () => {
  const older = request('completed', 'b'.repeat(32));
  older.created_at = '2026-10-14T09:00:00Z'; older.started_at = '2026-10-14T09:00:01Z';
  older.finished_at = '2026-10-14T09:00:02Z'; older.updated_at = older.finished_at;
  assert.equal(latestDiagnosticRequest(list([older, request()])).id, 'a'.repeat(32));
});

test('le proxy expose seulement la création, la liste et le détail en lecture', async () => {
  const source = await readFile(new URL('../nginx.conf.template', import.meta.url), 'utf8');
  assert.match(source, /location = \/api\/v1\/diagnostic-requests \{\s+limit_except GET POST/);
  const blocks = [...source.matchAll(/location ~ "([^"]*diagnostic-requests[^"]+)" \{([\s\S]*?)\n        \}/g)];
  assert.equal(blocks.length, 1);
  const route = new RegExp(blocks[0][1]);
  assert.match(blocks[0][2], /limit_except GET \{ deny all; \}/);
  assert.equal(route.test('/api/v1/diagnostic-requests/' + 'a'.repeat(32)), true);
  for (const path of ['/api/v1/diagnostic-requests', '/api/v1/diagnostic-requests/' + 'A'.repeat(32),
    '/api/v1/diagnostic-requests/' + 'a'.repeat(32) + '/execute', '/api/v1/diagnostic-requests/delete']) {
    assert.equal(route.test(path), false);
  }
});
