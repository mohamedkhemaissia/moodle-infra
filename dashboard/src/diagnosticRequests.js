const REQUEST_STATES = ['queued', 'running', 'completed', 'blocked', 'failed', 'timeout', 'canceled', 'interrupted'];
const TERMINAL_STATES = new Set(REQUEST_STATES.filter((state) => !['queued', 'running'].includes(state)));
const identifier = (value) => typeof value === 'string' && /^[0-9a-f]{32}$/.test(value);
const positive = (value) => Number.isSafeInteger(value) && value > 0;
const count = (value) => Number.isSafeInteger(value) && value >= 0;
const timestamp = (value) => typeof value === 'string' &&
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(value) && Number.isFinite(Date.parse(value));
const fail = () => { throw new Error('Le suivi des demandes de diagnostic est inexploitable.'); };

export const diagnosticRequestLabels = {
  queued: 'En attente', running: 'Diagnostic en cours', completed: 'Diagnostic terminé',
  blocked: 'Diagnostic bloqué', failed: 'Diagnostic en échec', timeout: 'Délai dépassé',
  canceled: 'Diagnostic annulé', interrupted: 'Diagnostic interrompu',
};

export function validateDiagnosticRequest(value, incidentId = null) {
  if (!value || !identifier(value.id) || !positive(value.incident_id) ||
      (incidentId !== null && value.incident_id !== incidentId) ||
      !REQUEST_STATES.includes(value.status) || !positive(value.requested_by?.id) ||
      typeof value.requested_by.username !== 'string' ||
      !/^[a-z][a-z0-9_.-]{2,31}$/.test(value.requested_by.username) ||
      !timestamp(value.created_at) || !timestamp(value.updated_at) ||
      Date.parse(value.updated_at) < Date.parse(value.created_at) ||
      (value.started_at !== null && (!timestamp(value.started_at) ||
        Date.parse(value.started_at) < Date.parse(value.created_at))) ||
      (value.finished_at !== null && (!timestamp(value.finished_at) ||
        Date.parse(value.finished_at) < Date.parse(value.started_at || value.created_at))) ||
      (value.reason_code !== null && (typeof value.reason_code !== 'string' ||
        !/^[a-z][a-z0-9_]{0,95}$/.test(value.reason_code))) ||
      (value.run_id !== null && !identifier(value.run_id)) ||
      value.result_url !== (value.run_id === null ? null : `/api/v1/diagnostics/${value.run_id}`)) fail();
  if (value.status === 'queued' && (value.started_at !== null || value.finished_at !== null)) fail();
  if (value.status === 'running' && (!value.started_at || value.finished_at !== null || value.run_id !== null)) fail();
  if (TERMINAL_STATES.has(value.status) && (!value.started_at || !value.finished_at ||
      (!value.run_id && !value.reason_code))) fail();
  return value;
}

function validateEnvelope(value) {
  if (value?.status !== 'available' || value.mode !== 'read_only' || !timestamp(value.evaluated_at)) fail();
}

export function validateDiagnosticRequests(value, incidentId) {
  validateEnvelope(value);
  if (!positive(incidentId) || !Array.isArray(value.items) || !count(value.total) ||
      !count(value.offset) || !positive(value.limit) || value.limit > 50 ||
      value.items.length !== Math.max(0, Math.min(value.limit, value.total - value.offset)) ||
      typeof value.has_more !== 'boolean' || value.has_more !== (value.offset + value.items.length < value.total)) fail();
  const ids = new Set();
  for (const item of value.items) {
    validateDiagnosticRequest(item, incidentId);
    if (ids.has(item.id)) fail();
    ids.add(item.id);
  }
  return value;
}

export function validateDiagnosticMutation(value, incidentId) {
  validateEnvelope(value);
  if (typeof value.created !== 'boolean') fail();
  validateDiagnosticRequest(value.request, incidentId);
  return value;
}

export function diagnosticRequestsPath(incidentId, offset = 0) {
  if (!positive(incidentId) || !count(offset) || offset > 10000) throw new Error('Incident ou page invalide.');
  return `/api/v1/diagnostic-requests?incident_id=${incidentId}&limit=10&offset=${offset}`;
}

export function diagnosticClientKey(crypto = globalThis.crypto) {
  if (!crypto?.getRandomValues) throw new Error('La demande exige un navigateur récent.');
  return [...crypto.getRandomValues(new Uint8Array(16))]
    .map((value) => value.toString(16).padStart(2, '0')).join('');
}

export function diagnosticActive(value) {
  return ['queued', 'running'].includes(value?.status);
}

export function latestDiagnosticRequest(value) {
  return value?.items?.slice().sort((left, right) =>
    Date.parse(right.created_at) - Date.parse(left.created_at) || right.id.localeCompare(left.id))[0] || null;
}
