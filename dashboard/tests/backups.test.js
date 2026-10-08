import assert from 'node:assert/strict';
import test from 'node:test';
import { validateBackupDocument, backupView } from '../src/backups.js';
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
