import { validateBackupDocument, validatePilotageBackupDocument } from './backups.js';
export const FRESH_MS = 60_000;
export const RESOURCES = [
  ['cpu_usage_percent', 'CPU utilisé'],
  ['memory_usage_percent', 'Mémoire utilisée'],
  ['disk_available_percent', 'Disque disponible'],
];
const states = ['running', 'completed', 'blocked', 'failed', 'timeout', 'canceled', 'interrupted'];
const finite = (x) => typeof x === 'number' && Number.isFinite(x);
const positiveId = (x) => Number.isSafeInteger(x) && x > 0;
const date = (x) => typeof x === 'string' && Number.isFinite(Date.parse(x));
const percent = (x) => finite(x) && x >= 0 && x <= 100;
const fail = () => { throw new Error('La réponse reçue est inexploitable.'); };

export function validateDocument(kind, value) {
  if (kind === 'backup') return validateBackupDocument(value);
  if (kind === 'pilotage_backup') return validatePilotageBackupDocument(value);
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail();
  if (kind === 'forecast' || kind === 'anomaly') {
    if (!['available', 'unavailable'].includes(value.status) || value.experimental !== true ||
        !date(value.evaluated_at) || !date(value.input_evaluated_at)) fail();
    if (value.status === 'available') {
      if (kind === 'forecast') {
        for (const horizon of ['5', '10', '15']) {
          const forecast = value.forecasts?.[horizon];
          if (!forecast || !date(forecast.target_evaluated_at)) fail();
          for (const [key] of RESOURCES) {
            const metric = forecast.metrics?.[key];
            if (!metric || !percent(metric.value) || !['ridge', 'persistence'].includes(metric.method)) fail();
          }
        }
      } else {
        const a = value.anomaly;
        if (!a || !finite(a.score) || !finite(a.threshold) ||
            typeof a.statistical_anomaly !== 'boolean' || typeof a.operational_rules?.alert !== 'boolean') fail();
      }
    }
  } else if (kind === 'summary') {
    if (value.status !== 'available' || !date(value.evaluated_at) ||
        !Number.isSafeInteger(value.counts?.open) || value.counts.open < 0 ||
        !Number.isSafeInteger(value.counts?.resolved) || value.counts.resolved < 0) fail();
  } else if (kind === 'incidents' || kind === 'runs') {
    if (!Array.isArray(value.items) || !Number.isSafeInteger(value.total) || value.total < 0 ||
        !Number.isSafeInteger(value.offset) || value.offset < 0 ||
        typeof value.has_more !== 'boolean') fail();
    for (const item of value.items) {
      if (!positiveId(item.incident_id ?? item.id)) fail();
      if (kind === 'incidents') {
        if (!positiveId(item.id) || typeof item.title !== 'string' || typeof item.severity !== 'string' ||
            !['open', 'resolved'].includes(item.status) || !date(item.opened_at) ||
            !date(item.last_observed_at)) fail();
      } else if (!/^[a-f0-9]{32}$/.test(item.id) || !states.includes(item.status) ||
                 !finite(item.started_at) || item.started_at < 0) fail();
    }
  } else if (kind === 'decision') {
    if (!date(value.evaluated_at) || !positiveId(value.decision?.incident_id) ||
        !['recommendation', 'blocked', 'monitoring', 'closed'].includes(value.decision.status) ||
        typeof value.decision.explanation !== 'string' || typeof value.decision.policy_version !== 'string' ||
        !Array.isArray(value.decision.reason_codes) || !value.decision.reason_codes.every((x) => typeof x === 'string')) fail();
    const action = value.decision.recommended_action;
    if (action && (typeof action.title !== 'string' || !Array.isArray(action.steps) ||
                   !action.steps.every((step) => typeof step === 'string'))) fail();
  } else if (kind === 'run') {
    if (!/^[a-f0-9]{32}$/.test(value.id) || !positiveId(value.incident_id) || !states.includes(value.status)) fail();
    if (value.report?.reason !== undefined && typeof value.report.reason !== 'string') fail();
  } else fail();
  return value;
}

export function freshResult(result, now) {
  if (!result?.data || result.error) return null;
  const age = now - result.receivedAt;
  if (!Number.isFinite(age) || age < 0 || age > FRESH_MS) return null;
  if (result.data.evaluated_at) {
    const sourceAge = now - Date.parse(result.data.evaluated_at);
    if (!Number.isFinite(sourceAge) || sourceAge < -30_000 || sourceAge > FRESH_MS) return null;
  }
  return result.data;
}

export function analysisReady(value, now) {
  if (value?.status !== 'available') return false;
  const source = value.collection?.linux;
  const inputAge = now - Date.parse(value.input_evaluated_at);
  const responseAge = now - Date.parse(value.evaluated_at);
  return source?.status === 'up' && finite(source.scrape_age_seconds) && source.scrape_age_seconds >= 0 &&
    source.scrape_age_seconds * 1000 + Math.max(0, responseAge) <= FRESH_MS &&
    inputAge >= -30_000 && inputAge <= FRESH_MS;
}

export function decisionCurrent(value, now) {
  const d = value?.decision;
  return value?.snapshot_usable === true && d?.status === 'recommendation' &&
    date(d.input_evaluated_at) && date(d.valid_until) &&
    Date.parse(d.input_evaluated_at) <= now && now < Date.parse(d.valid_until);
}

export function recoveryProven(value) {
  return value?.status === 'completed' && value.report?.action_dispatched === true &&
    value.report?.recovered === true && value.report?.reason === 'https_and_new_probe_confirmed';
}

export function requestPath(kind, { status = '', offset = 0, incidentId = null, runId = null } = {}) {
  if (!Number.isSafeInteger(offset) || offset < 0 || offset > 100000 ||
      (incidentId !== null && !positiveId(incidentId))) throw new Error('Sélection invalide.');
  const summaries = { forecast: 'forecasts/summary', anomaly: 'anomalies/summary', summary: 'incidents/summary' };
  if (summaries[kind]) return '/api/v1/' + summaries[kind];
  if (kind === 'incidents') {
    if (!['', 'open', 'resolved'].includes(status)) throw new Error('Filtre invalide.');
    return `/api/v1/incidents?limit=10&offset=${offset}${status ? '&status=' + status : ''}`;
  }
  if (kind === 'decision' && incidentId !== null) return `/api/v1/decisions/${incidentId}`;
  if (kind === 'diagnostics' || kind === 'remediations') {
    if (runId !== null) {
      if (!/^[a-f0-9]{32}$/.test(runId)) throw new Error('Exécution invalide.');
      return `/api/v1/${kind}/${runId}`;
    }
    return `/api/v1/${kind}?limit=10&offset=${offset}${incidentId === null ? '' : '&incident_id=' + incidentId}`;
  }
  throw new Error('Lecture non autorisée.');
}
