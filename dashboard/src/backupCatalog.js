const finite = (value) => typeof value === 'number' && Number.isFinite(value);
const integer = (value, max) => Number.isSafeInteger(value) && value >= 0 && value <= max;
const hash = (value) => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const date = (value) => typeof value === 'string' &&
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value) &&
  Number.isFinite(Date.parse(value)) && new Date(value).toISOString() === value.replace('Z', '.000Z');
const fail = () => { throw new Error('Le catalogue des points de reprise est inexploitable.'); };

export function validateCatalog(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      !['available', 'unavailable'].includes(value.status) || value.mode !== 'read_only' ||
      !['moodle', 'pilotage'].includes(value.scope) || !date(value.evaluated_at) ||
      !integer(value.limit, 50) || value.limit === 0 || !integer(value.offset, 200) ||
      !integer(value.listed_total, 200) || typeof value.has_more !== 'boolean' ||
      typeof value.truncated !== 'boolean' || !Array.isArray(value.items) ||
      value.items.length > value.limit ||
      !['present', 'absent', 'unknown'].includes(value.latest_success_presence) ||
      (value.latest_success_id !== null && !hash(value.latest_success_id)) ||
      value.verification?.repository_integrity !== 'not_checked' ||
      value.verification?.restoration !== 'not_checked') fail();
  const collection = value.collection;
  if (!collection || !['up', 'stale', 'missing', 'invalid'].includes(collection.status) ||
      collection.max_age_seconds !== 900 ||
      (collection.observed_at !== null && !date(collection.observed_at)) ||
      (collection.age_seconds !== null && !finite(collection.age_seconds))) fail();
  if (value.status !== 'available') {
    if (value.total !== null || value.listed_total !== 0 || value.items.length !== 0 ||
        value.has_more || value.truncated || value.latest_success_id !== null ||
        value.latest_success_presence !== 'unknown' || typeof value.reason !== 'string') fail();
    return value;
  }
  if (value.reason !== null || !integer(value.total, 10000) ||
      value.listed_total !== Math.min(value.total, 200) || value.truncated !== (value.total > 200) ||
      value.has_more !== (value.offset + value.limit < value.listed_total) ||
      value.items.length !== Math.max(0, Math.min(value.limit, value.listed_total - value.offset)) ||
      collection.status !== 'up' || !date(collection.observed_at) || !finite(collection.age_seconds) ||
      collection.age_seconds < 0 || collection.age_seconds > 900 ||
      Date.parse(collection.observed_at) > Date.parse(value.evaluated_at) ||
      Math.abs((Date.parse(value.evaluated_at)-Date.parse(collection.observed_at))/1000-collection.age_seconds) > 1 ||
      (value.latest_success_id === null) !== (value.latest_success_presence === 'unknown')) fail();
  const identifiers = new Set();
  let previous = null;
  for (const point of value.items) {
    if (!point || Object.keys(point).sort().join(',') !== 'archived_at,hostname,id,recovery_at,success_recorded' ||
        !hash(point.id) || identifiers.has(point.id) || !date(point.archived_at) ||
        Date.parse(point.archived_at) > Date.parse(collection.observed_at) ||
        typeof point.hostname !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,253}$/.test(point.hostname) ||
        typeof point.success_recorded !== 'boolean' || point.success_recorded !==
          (point.id === value.latest_success_id && value.latest_success_presence === 'present')) fail();
    if (point.success_recorded ? (!date(point.recovery_at) || Date.parse(point.recovery_at) > Date.parse(point.archived_at)) :
      point.recovery_at !== null) fail();
    const order = point.archived_at + point.id;
    if (previous !== null && previous < order) fail();
    previous = order;
    identifiers.add(point.id);
  }
  if (identifiers.has(value.latest_success_id) && value.latest_success_presence !== 'present') fail();
  return value;
}

export function catalogCurrent(value, now) {
  if (value?.status !== 'available' || !finite(now)) return false;
  const responseAge = now - Date.parse(value.evaluated_at);
  return responseAge >= -30000 && responseAge <= 60000 &&
    value.collection.status === 'up' && value.collection.age_seconds + Math.max(0, responseAge/1000) <= 900;
}

export function catalogPath(scope, offset = 0) {
  if (!['moodle', 'pilotage'].includes(scope) || !integer(offset, 200)) throw new Error('Sélection invalide.');
  return `/api/v1/backups/catalog?scope=${scope}&limit=10&offset=${offset}`;
}
