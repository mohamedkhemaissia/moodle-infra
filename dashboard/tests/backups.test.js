import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { validateBackupDocument, validatePilotageBackupDocument, backupView, pilotageRestoreView } from '../src/backups.js';
import { validateDocument, freshResult } from '../src/operations.js';

const now = Date.parse('2026-10-08T21:00:00Z');
const document = () => ({
  status: 'available', mode: 'read_only', evaluated_at: '2026-10-08T21:00:00Z',
  backup_state: 'recent', reason: null,
  latest_success: { snapshot: 'a'.repeat(64), frozen_at_utc: '2026-10-08T09:00:00Z',
    completed_at_utc: '2026-10-08T09:05:00Z', age_hours: 12, duration_seconds: 300 },
  policy: { max_age_hours: 24 }, schedule: { state: 'active', enabled: true },
  job: { state: 'idle', last_result: 'success' },
  collection: { status: 'up', observed_at: '2026-10-08T21:00:00Z', age_seconds: 0, max_age_seconds: 150 },
});
test('a recent point is shown with its age measured from the freeze', () => {
  const data = validateDocument('backup', document());
  assert.equal(backupView(data, now).state, 'recent');
  assert.equal(backupView(data, now).ageHours, 12);
});

test('an overdue point is preserved as overdue', () => {
  const data = document();
  data.backup_state = 'overdue';
  data.latest_success.frozen_at_utc = '2026-10-07T09:00:00Z';
  data.latest_success.completed_at_utc = '2026-10-07T09:05:00Z';
  data.latest_success.age_hours = 36;
  assert.equal(backupView(validateBackupDocument(data), now).state, 'overdue');
});

test('an old response or interrupted collector cannot remain recent', () => {
  const data = document();
  assert.equal(backupView(data, now + 61_000).state, 'unknown');
  assert.equal(backupView(data, now - 31_000).state, 'unknown');
  data.collection.age_seconds = 140;
  assert.equal(backupView(data, now + 11_000).state, 'unknown');
  assert.equal(freshResult({ data, receivedAt: now }, now + 61_000), null);
});

test('missing points and unavailable follow-up remain distinct', () => {
  const data = document();
  data.backup_state = 'missing';
  data.latest_success = null;
  data.reason = 'success_marker_missing';
  assert.equal(backupView(validateBackupDocument(data), now).state, 'missing');
  data.status = 'unavailable';
  data.backup_state = 'unknown';
  data.collection = { status: 'missing', observed_at: null, age_seconds: null, max_age_seconds: 150 };
  assert.equal(backupView(validateBackupDocument(data), now).state, 'unknown');
});

test('malformed dates, statuses and nonfinite or incoherent measurements are rejected', () => {
  for (const mutate of [
    (d) => { d.policy.max_age_hours = Infinity; },
    (d) => { d.collection.age_seconds = NaN; },
    (d) => { d.schedule.enabled = 'yes'; },
    (d) => { d.latest_success.frozen_at_utc = 'bad'; },
    (d) => { d.latest_success.snapshot = 'invalid'; },
    (d) => { d.latest_success.completed_at_utc = '2026-10-09T09:00:00Z'; },
    (d) => { d.job.last_result = 'guaranteed'; },
  ]) {
    const data = document();
    mutate(data);
    assert.throws(() => validateBackupDocument(data));
  }
});

test('a point reaching the age threshold changes state between polls', () => {
  const data = document();
  data.policy.max_age_hours = 12;
  assert.equal(backupView(data, now).state, 'recent');
  assert.equal(backupView(data, now + 1000).state, 'overdue');
});

export const pilotageDocument = () => ({
  ...document(), scope: 'pilotage_postgresql', restoration_state: 'validated',
  restore_collection_status: 'recorded', same_snapshot: true,
  latest_success: { ...document().latest_success, tables: 16, rows: 415, repository_check: 'passed' },
  latest_restore: { status: 'restore_validated', run_id: 'c'.repeat(32), snapshot: 'a'.repeat(64),
    started_at: '2026-10-08T09:15:00Z', completed_at: '2026-10-08T09:16:17Z',
    captured_at: '2026-10-08T09:00:00Z', duration_seconds: 76.819, tables_verified: 16,
    rows_verified: 415, sequence_check: 'passed', cleanup: { container_removed: true, temporary_files_removed: true } },
});

test('both backup routes are forwarded by the dashboard proxy with GET only', async () => {
  const template = await readFile(new URL('../nginx.conf.template', import.meta.url), 'utf8');
  const match = template.match(/location ~ "([^"]+)" \{([\s\S]*?)\n        \}/);
  assert.ok(match, 'Read proxy location must exist');
  const route = new RegExp(match[1]);
  for (const path of ['/api/v1/backups/summary', '/api/v1/backups/pilotage/summary']) {
    assert.equal(route.test(path), true, 'Dashboard route must be forwarded: ' + path);
  }
  for (const path of ['/api/v1/backups/pilotage/restore', '/api/v1/backups/pilotage/summary/extra',
    '/api/v1/backups/anything/summary', '/api/v1/unknown']) {
    assert.equal(route.test(path), false, 'Unexpected route must remain denied: ' + path);
  }
  assert.match(match[2], /limit_except GET \{ deny all; \}/);
  assert.match(match[2], /proxy_pass \$\{API_ORIGIN\};/);
  assert.match(template, /location \/api\/ \{ return 404; \}/);
});

test('a restore validates exactly the same snapshot and inventory', () => {
  const data = validateDocument('pilotage_backup', pilotageDocument());
  assert.equal(pilotageRestoreView(data, now).state, 'validated');
  for (const mutate of [
    (d) => { d.latest_success.snapshot = 'b'.repeat(64); },
    (d) => { d.latest_restore.rows_verified = 414; },
    (d) => { d.latest_restore.captured_at = '2026-10-08T08:59:59Z'; },
    (d) => { d.latest_restore.cleanup.temporary_files_removed = false; },
    (d) => { d.latest_success.tables = true; },
    (d) => { d.latest_restore.duration_seconds = Infinity; },
    (d) => { d.latest_restore.sequence_check = 'not_verified'; },
    (d) => { d.latest_restore.completed_at = '2026-10-09T09:00:00Z'; },
  ]) {
    const value = pilotageDocument();
    mutate(value);
    assert.throws(() => validatePilotageBackupDocument(value));
  }
});

test('a successful test on a different point remains historical', () => {
  const data = pilotageDocument();
  data.latest_success.snapshot = 'b'.repeat(64);
  data.same_snapshot = false;
  data.restoration_state = 'not_tested';
  const view = pilotageRestoreView(validatePilotageBackupDocument(data), now);
  assert.equal(view.state, 'not_tested');
  assert.equal(view.test.status, 'restore_validated');
  assert.equal(view.sameSnapshot, false);
});

test('a failed current test remains failed and cannot be shown as validated', () => {
  const data = pilotageDocument();
  data.latest_restore.status = 'failed';
  data.latest_restore.tables_verified = null;
  data.latest_restore.rows_verified = null;
  data.latest_restore.duration_seconds = null;
  data.latest_restore.captured_at = null;
  data.latest_restore.sequence_check = 'not_verified';
  data.restoration_state = 'failed';
  assert.equal(pilotageRestoreView(validatePilotageBackupDocument(data), now).state, 'failed');
  data.restoration_state = 'validated';
  assert.throws(() => validatePilotageBackupDocument(data));
});

test('missing and invalid restore reports stay distinct', () => {
  const data = pilotageDocument();
  data.latest_restore = null;
  data.same_snapshot = null;
  data.restore_collection_status = 'missing';
  data.restoration_state = 'not_tested';
  assert.equal(pilotageRestoreView(validatePilotageBackupDocument(data), now).state, 'not_tested');
  data.restore_collection_status = 'invalid';
  data.restoration_state = 'unknown';
  assert.equal(pilotageRestoreView(validatePilotageBackupDocument(data), now).state, 'unknown');
});

test('stale responses hide the restoration proof as well as the backup health', () => {
  const data = validatePilotageBackupDocument(pilotageDocument());
  assert.equal(pilotageRestoreView(data, now + 61_000).state, 'unknown');
  assert.equal(pilotageRestoreView(data, now + 61_000).test, null);
  data.collection.age_seconds = 140;
  assert.equal(pilotageRestoreView(data, now + 11_000).test, null);
});
