export const SERVICE_KEYS = {
  php_fpm: ['active_processes', 'idle_processes', 'total_processes', 'listen_queue', 'max_children_reached'],
  postgresql: ['database_connections', 'server_connections', 'max_connections', 'commits_per_second', 'rollbacks_per_second', 'deadlocks_total'],
};
const finite = (v) => typeof v === 'number' && Number.isFinite(v);
const fail = () => { throw new Error('Les indicateurs des services sont inexploitables.'); };

export function validateApplications(value) {
  if (!value || value.source !== 'prometheus' || value.mode !== 'read_only' ||
      !['available', 'partial', 'unavailable'].includes(value.status) ||
      typeof value.evaluated_at !== 'string' || !Number.isFinite(Date.parse(value.evaluated_at)) ||
      !finite(value.max_scrape_age_seconds) || value.max_scrape_age_seconds <= 0) fail();
  for (const [name, keys] of Object.entries(SERVICE_KEYS)) {
    const service = value.services?.[name];
    const c = service?.collection;
    if (!service || !['available', 'partial', 'unavailable'].includes(service.status) ||
        !c || !['up', 'down', 'stale', 'missing'].includes(c.status) ||
        (c.scrape_age_seconds !== null && (!finite(c.scrape_age_seconds) || c.scrape_age_seconds < 0)) ||
        ![true, false, null].includes(service.service_reachable) || !service.metrics ||
        !Array.isArray(service.unavailable_metrics)) fail();
    for (const key of keys) {
      const v = service.metrics[key];
      if (v !== null && (!finite(v) || v < 0)) fail();
      if ((v === null) !== service.unavailable_metrics.includes(key)) fail();
      if (v !== null && (c.status !== 'up' || service.service_reachable !== true ||
          c.scrape_age_seconds === null || c.scrape_age_seconds > value.max_scrape_age_seconds)) fail();
    }
    const missing = keys.filter((key) => service.metrics[key] === null).length;
    const expected = missing === 0 ? 'available' : missing === keys.length ? 'unavailable' : 'partial';
    if (service.status !== expected || service.unavailable_metrics.length !== missing) fail();
  }
  const services = Object.values(value.services);
  const expected = services.every((s) => s.status === 'available') ? 'available' :
    services.every((s) => s.status === 'unavailable') ? 'unavailable' : 'partial';
  if (value.status !== expected) fail();
  return value;
}

export function applicationView(document, name, now) {
  const service = document?.services?.[name];
  const delta = now - Date.parse(document?.evaluated_at);
  const elapsed = Math.max(0, delta / 1000);
  const responseFresh = finite(now) && finite(delta) && delta >= -30_000 && delta <= 60_000;
  const ageFresh = finite(service?.collection.scrape_age_seconds) &&
    service.collection.scrape_age_seconds + elapsed <= document.max_scrape_age_seconds;
  const fresh = responseFresh &&
    service?.collection.status === 'up' && finite(service.collection.scrape_age_seconds) &&
    service.collection.scrape_age_seconds + elapsed <= document.max_scrape_age_seconds;
  return { collection: fresh ? 'up' : responseFresh && ageFresh && service?.collection.status === 'down' ? 'down' : 'unknown',
    reachable: fresh ? service.service_reachable : null,
    metrics: Object.fromEntries((SERVICE_KEYS[name] || []).map((key) => [key,
      fresh && service.service_reachable === true ? service.metrics[key] : null])) };
}
