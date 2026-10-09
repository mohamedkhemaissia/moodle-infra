const finite = (value) => typeof value === 'number' && Number.isFinite(value);
const date = (value) => typeof value === 'string' &&
  /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/.test(value) &&
  Number.isFinite(Date.parse(value));
const fail = () => { throw new Error('La réponse du suivi des sauvegardes est inexploitable.'); };

export function validateBackupDocument(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      !['available', 'unavailable'].includes(value.status) || value.mode !== 'read_only' ||
      !date(value.evaluated_at) ||
      !['recent', 'overdue', 'missing', 'unknown'].includes(value.backup_state)) fail();
  const c = value.collection;
  if (!c || !['up', 'stale', 'missing', 'invalid'].includes(c.status) ||
      c.max_age_seconds !== 150 ||
      (c.observed_at !== null && !date(c.observed_at)) ||
      (c.age_seconds !== null && !finite(c.age_seconds))) fail();
  if (value.status === 'available') {
    if (c.status !== 'up' || !date(c.observed_at) || !finite(c.age_seconds) ||
        c.age_seconds < 0 || c.age_seconds > 150 ||
        !finite(value.policy?.max_age_hours) || value.policy.max_age_hours <= 0 ||
        value.policy.max_age_hours > 720 ||
        !['active', 'inactive', 'unknown'].includes(value.schedule?.state) ||
        (value.schedule.enabled !== null && typeof value.schedule.enabled !== 'boolean') ||
        !['running', 'idle', 'unknown'].includes(value.job?.state) ||
        !['success', 'failed', 'unknown'].includes(value.job.last_result)) fail();
    if (['recent', 'overdue'].includes(value.backup_state)) {
      const p = value.latest_success;
      if (!p || !/^[a-f0-9]{64}$/.test(p.snapshot) || !date(p.frozen_at_utc) ||
          !date(p.completed_at_utc) || !finite(p.age_hours) || p.age_hours < 0 ||
          !finite(p.duration_seconds) || p.duration_seconds < 0 ||
          Date.parse(p.frozen_at_utc) > Date.parse(p.completed_at_utc) ||
          Date.parse(p.completed_at_utc) > Date.parse(c.observed_at)) fail();
    } else if (value.latest_success !== null) fail();
  } else if (value.backup_state !== 'unknown' || value.latest_success !== null) fail();
  return value;
}

export function backupView(value, now) {
  if (!value || value.status !== 'available' || !finite(now)) return { state: 'unknown', reason: value?.reason };
  const difference = now - Date.parse(value.evaluated_at);
  const elapsed = Math.max(0, difference / 1000);
  if (difference < -30_000 || difference > 60_000 ||
      value.collection.status !== 'up' || value.collection.age_seconds + elapsed > 150) {
    return { state: 'unknown', reason: 'collection_stale' };
  }
  if (!value.latest_success) return { state: value.backup_state, reason: value.reason };
  const ageHours = (Date.parse(value.evaluated_at) - Date.parse(value.latest_success.frozen_at_utc)) /
    3_600_000 + elapsed / 3600;
  const state = value.backup_state === 'overdue' || ageHours > value.policy.max_age_hours ? 'overdue' : 'recent';
  return { state, ageHours, reason: null };
}

const count = (value, minimum = 0) => Number.isSafeInteger(value) && value >= minimum;
const hash = (value, length) => typeof value === 'string' &&
  new RegExp('^[a-f0-9]{' + length + '}$').test(value);

export function validatePilotageBackupDocument(value) {
  validateBackupDocument(value);
  if (value.scope !== 'pilotage_postgresql' ||
      !['unknown', 'recorded', 'missing', 'invalid'].includes(value.restore_collection_status) ||
      !['unknown', 'validated', 'not_tested', 'failed', 'inconclusive'].includes(value.restoration_state) ||
      (value.same_snapshot !== null && typeof value.same_snapshot !== 'boolean')) fail();
  if (value.status !== 'available') {
    if (value.latest_restore !== null || value.restoration_state !== 'unknown' || value.same_snapshot !== null) fail();
    return value;
  }
  const p = value.latest_success;
  if (p && (!count(p.tables, 1) || !count(p.rows) || p.repository_check !== 'passed')) fail();
  const t = value.latest_restore;
  if (value.restore_collection_status === 'recorded') {
    if (!t || !hash(t.run_id, 32) || !hash(t.snapshot, 64) ||
        !['restore_validated', 'failed', 'inconclusive'].includes(t.status) ||
        !date(t.started_at) || !date(t.completed_at) ||
        Date.parse(t.started_at) > Date.parse(t.completed_at) ||
        Date.parse(t.completed_at) > Date.parse(value.collection.observed_at) ||
        (t.captured_at !== null && (!date(t.captured_at) || Date.parse(t.captured_at) > Date.parse(t.started_at))) ||
        (t.duration_seconds !== null && (!finite(t.duration_seconds) || t.duration_seconds < 0)) ||
        (t.tables_verified !== null && !count(t.tables_verified, 1)) ||
        (t.rows_verified !== null && !count(t.rows_verified)) ||
        !['passed', 'not_verified'].includes(t.sequence_check) ||
        typeof t.cleanup?.container_removed !== 'boolean' ||
        typeof t.cleanup?.temporary_files_removed !== 'boolean') fail();
    if (t.status === 'restore_validated' && (!date(t.captured_at) || !finite(t.duration_seconds) ||
        !count(t.tables_verified, 1) || !count(t.rows_verified) || t.sequence_check !== 'passed' ||
        !t.cleanup.container_removed || !t.cleanup.temporary_files_removed)) fail();
  } else if (t !== null) fail();
  const same = p && t ? p.snapshot === t.snapshot : null;
  let state = p && value.restore_collection_status !== 'invalid' ? 'not_tested' : 'unknown';
  if (same) {
    state = t.status === 'restore_validated' ?
      (t.captured_at === p.frozen_at_utc && t.tables_verified === p.tables && t.rows_verified === p.rows ?
        'validated' : 'unknown') : t.status;
  }
  if (value.same_snapshot !== same || value.restoration_state !== state) fail();
  return value;
}

export function pilotageRestoreView(value, now) {
  const view = backupView(value, now);
  if (!value || value.status !== 'available' || view.reason === 'collection_stale') {
    return { state: 'unknown', test: null, sameSnapshot: null };
  }
  return { state: value.restoration_state, test: value.latest_restore, sameSnapshot: value.same_snapshot };
}
