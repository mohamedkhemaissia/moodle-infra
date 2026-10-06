export const MAX_AGE_MS = 60_000;
export const REFRESH_MS = 15_000;
export const METRIC_KEYS = [
  'cpu_usage_percent',
  'memory_usage_percent',
  'disk_available_percent',
  'moodle_available',
  'moodle_probe_duration_ms',
  'moodle_http_status_code',
];

export function validateSnapshot(data) {
  const invalid = () => {
    throw new Error('Les données reçues ne sont pas exploitables.');
  };
  if (
    !data ||
    data.source !== 'prometheus' ||
    typeof data.evaluated_at !== 'string' ||
    !Number.isFinite(Date.parse(data.evaluated_at)) ||
    !data.metrics ||
    !Array.isArray(data.unavailable_metrics) ||
    !data.unavailable_metrics.every((key) => typeof key === 'string')
  )
    invalid();
  for (const key of METRIC_KEYS) {
    const value = data.metrics[key];
    if (value === null) continue;
    if (key === 'moodle_available') {
      if (typeof value !== 'boolean') invalid();
    } else {
      if (typeof value !== 'number' || !Number.isFinite(value) || value < 0)
        invalid();
      if (key.endsWith('_percent') && value > 100) invalid();
      if (
        key === 'moodle_http_status_code' &&
        (!Number.isInteger(value) ||
          (value !== 0 && (value < 100 || value > 599)))
      )
        invalid();
    }
  }
  for (const job of ['linux', 'moodle_https']) {
    const source = data.collection?.[job];
    if (!source || !['up', 'down', 'stale', 'missing'].includes(source.status))
      invalid();
    if (
      source.scrape_age_seconds !== null &&
      (typeof source.scrape_age_seconds !== 'number' ||
        !Number.isFinite(source.scrape_age_seconds) ||
        source.scrape_age_seconds < 0)
    )
      invalid();
  }
  return data;
}

export function buildView(snapshot, now = Date.now(), error = null) {
  const age = snapshot ? now - Date.parse(snapshot.evaluated_at) : null;
  const reason = error
    ? 'error'
    : !snapshot
      ? 'pending'
      : age < -30_000
        ? 'clock'
        : age > MAX_AGE_MS
          ? 'stale'
          : 'ready';
  const collection = {};
  for (const job of ['linux', 'moodle_https']) {
    const source = snapshot?.collection[job];
    const effectiveAge =
      source?.scrape_age_seconds === null || !source
        ? null
        : source.scrape_age_seconds + Math.max(0, age / 1000);
    const status =
      reason !== 'ready'
        ? 'unknown'
        : source.status === 'up' && effectiveAge === null
          ? 'missing'
          : source.status === 'up' && effectiveAge > MAX_AGE_MS / 1000
            ? 'stale'
            : source.status;
    collection[job] = { status, age: effectiveAge };
  }
  const metrics = {};
  for (const key of METRIC_KEYS) {
    const job = key.startsWith('moodle_') ? 'moodle_https' : 'linux';
    metrics[key] =
      collection[job].status === 'up' ? snapshot.metrics[key] : null;
  }
  return { reason, collection, metrics, age };
}

export function sampleFromView(view, time) {
  return {
    time,
    cpu_usage_percent: view.metrics.cpu_usage_percent,
    memory_usage_percent: view.metrics.memory_usage_percent,
    disk_available_percent: view.metrics.disk_available_percent,
  };
}

export function chartSeries(samples, key, width = 680, height = 164) {
  if (!samples.length) return { path: '', lastPoint: null };
  const firstTime = samples[0].time;
  const lastTime = samples.at(-1).time;
  const span = Math.max(lastTime - firstTime, REFRESH_MS);
  let path = '';
  let connected = false;
  let previousTime = null;
  let lastPoint = null;
  for (const sample of samples) {
    const value = sample[key];
    if (value === null || !Number.isFinite(value)) {
      connected = false;
      lastPoint = null;
      continue;
    }
    const x = ((sample.time - firstTime) / span) * width;
    const y = height - (value / 100) * height;
    if (previousTime !== null && sample.time - previousTime > REFRESH_MS * 3)
      connected = false;
    path += `${connected ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)} `;
    connected = true;
    previousTime = sample.time;
    lastPoint = { x, y };
  }
  return { path: path.trim(), lastPoint };
}
