export const CONTAINER_KEYS = ['cpu_usage_percent', 'memory_working_set_bytes', 'memory_limit_bytes', 'pids'];
const finite = (v) => typeof v === 'number' && Number.isFinite(v);
const states = ['unknown', 'missing', 'ambiguous', 'running', 'paused', 'not_running'];
const sourceStates = ['up', 'down', 'stale', 'missing'];
const label = (v) => typeof v === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$/.test(v);
const age = (v) => v === null || (finite(v) && v >= 0);
const fail = () => { throw new Error('Le suivi des conteneurs est inexploitable.'); };

export function validateContainers(value) {
  if (!value || value.source !== 'prometheus' || value.mode !== 'read_only' ||
      !['available', 'partial', 'unavailable'].includes(value.status) ||
      typeof value.evaluated_at !== 'string' || !Number.isFinite(Date.parse(value.evaluated_at)) ||
      !finite(value.max_scrape_age_seconds) || value.max_scrape_age_seconds <= 0 ||
      !finite(value.max_collection_age_seconds) || value.max_collection_age_seconds <= 0 ||
      !sourceStates.includes(value.collection?.status) || !age(value.collection?.scrape_age_seconds) ||
      !sourceStates.includes(value.collector?.status) || !age(value.collector?.age_seconds) ||
      !Array.isArray(value.containers) || value.containers.length < 1 || value.containers.length > 24) fail();
  const usable = value.collection.status === 'up' && value.collector.status === 'up' &&
    value.collection.scrape_age_seconds !== null && value.collection.scrape_age_seconds <= value.max_scrape_age_seconds &&
    value.collector.age_seconds !== null && value.collector.age_seconds <= value.max_collection_age_seconds;
  const identities = new Set();
  for (const row of value.containers) {
    if (!row || !label(row.project) || !label(row.service) || identities.has(`${row.project}/${row.service}`) ||
        !states.includes(row.state) || ![null, 'healthy', 'unhealthy', 'starting'].includes(row.health) ||
        !age(row.stats_age_seconds) || (row.instances !== null && (!Number.isSafeInteger(row.instances) || row.instances < 0)) ||
        !['available', 'partial', 'unavailable'].includes(row.metrics_status) || !row.metrics) fail();
    identities.add(`${row.project}/${row.service}`);
    if (!usable && (row.state !== 'unknown' || row.instances !== null || row.health !== null)) fail();
    if (row.state === 'missing' && row.instances !== 0) fail();
    if (row.state === 'ambiguous' && !(row.instances > 1)) fail();
    if (['running', 'paused', 'not_running'].includes(row.state) && row.instances !== 1) fail();
    if (row.health !== null && row.state !== 'running') fail();
    for (const key of CONTAINER_KEYS) {
      const v = row.metrics[key];
      if (v !== null && (!finite(v) || v < 0 || !usable || row.state !== 'running' ||
          row.stats_age_seconds === null || row.stats_age_seconds > value.max_collection_age_seconds)) fail();
    }
    const count = CONTAINER_KEYS.filter((key) => row.metrics[key] !== null).length;
    if (row.metrics_status !== (count === 4 ? 'available' : count === 0 ? 'unavailable' : 'partial')) fail();
  }
  const known = value.containers.map((r) => !['unknown', 'ambiguous'].includes(r.state) &&
    (r.state !== 'running' || r.metrics_status === 'available'));
  if (value.status !== (known.every(Boolean) ? 'available' : value.containers.some((r) => r.state !== 'unknown') ? 'partial' : 'unavailable')) fail();
  return value;
}

export function containerView(document, now) {
  const delta = now - Date.parse(document?.evaluated_at);
  const elapsed = Math.max(0, delta / 1000);
  const responseFresh = finite(now) && finite(delta) && delta >= -30_000 && delta <= 60_000;
  const usable = responseFresh && document?.collection.status === 'up' && document?.collector.status === 'up' &&
    finite(document.collection.scrape_age_seconds) && document.collection.scrape_age_seconds + elapsed <= document.max_scrape_age_seconds &&
    finite(document.collector.age_seconds) && document.collector.age_seconds + elapsed <= document.max_collection_age_seconds;
  return { fresh: usable, rows: (document?.containers || []).map((row) => {
    const statsFresh = usable && row.state === 'running' && finite(row.stats_age_seconds) &&
      row.stats_age_seconds + elapsed <= document.max_collection_age_seconds;
    return { ...row, state: usable ? row.state : 'unknown', health: usable ? row.health : null,
      metrics: Object.fromEntries(CONTAINER_KEYS.map((key) => [key, statsFresh ? row.metrics[key] : null])) };
  }) };
}
