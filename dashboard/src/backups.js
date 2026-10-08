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
