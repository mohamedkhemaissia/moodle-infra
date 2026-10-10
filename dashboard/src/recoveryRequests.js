const fail = () => { throw new Error('Le journal des demandes est inexploitable.'); };
const id = (v, size = 32) => typeof v === 'string' && new RegExp(`^[0-9a-f]{${size}}$`).test(v);
const integer = (v) => Number.isSafeInteger(v) && v >= 0;
const date = (v) => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(v)
  && Number.isFinite(Date.parse(v)) && new Date(v).toISOString() === v.replace('Z', '.000Z');
const actor = (v) => integer(v?.id) && v.id > 0 && /^[a-z][a-z0-9_.-]{2,31}$/.test(v.username);
const note = (v) => typeof v === 'string' && v.trim().length >= 15 && v.length <= 500 && !/[\x00-\x1f\x7f]/.test(v);
export const requestLabels = { pending: 'En attente', approved: 'Approuvée', rejected: 'Refusée', cancelled: 'Annulée' };

export function validateRecoveryRequest(row) {
  if (!row || !id(row.id) || !id(row.snapshot_id, 64) || !['moodle', 'pilotage'].includes(row.scope)
      || row.target !== 'isolated_test' || !note(row.reason) || !Object.hasOwn(requestLabels, row.status)
      || !integer(row.revision) || row.revision < 1 || !actor(row.requested_by)
      || !date(row.created_at) || !date(row.updated_at) || Date.parse(row.updated_at) < Date.parse(row.created_at)
      ) fail();
  validateExecution(row.execution);
  const selection = row.selection;
  const point = selection?.point;
  if (selection?.scope !== row.scope || !date(selection.catalog_observed_at) || point?.id !== row.snapshot_id
      || !date(point.archived_at) || Date.parse(point.archived_at) > Date.parse(selection.catalog_observed_at)
      || typeof point.hostname !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,253}$/.test(point.hostname)
      || typeof point.success_recorded !== 'boolean'
      || (point.success_recorded ? !date(point.recovery_at) || Date.parse(point.recovery_at) > Date.parse(point.archived_at)
        : point.recovery_at !== null)) fail();
  if (row.status === 'pending') { if (row.decision !== null) fail(); }
  else {
    const decision = row.decision;
    if (!actor(decision?.by) || !note(decision.reason) || !date(decision.at)
        || Date.parse(decision.at) !== Date.parse(row.updated_at) || typeof decision.approval_valid !== 'boolean') fail();
    if (row.status === 'approved') {
      if (!date(decision.valid_until) || Date.parse(decision.valid_until) - Date.parse(decision.at) !== 900000) fail();
    } else if (decision.valid_until !== null || decision.approval_valid !== false) fail();
  }
  return row;
}

export function validateRecoveryDocument(kind, value) {
  validateEnvelope(value);
  if (kind === 'recovery_requests') {
    if (!Array.isArray(value.items) || !integer(value.total) || !integer(value.limit) || value.limit < 1 || value.limit > 50
        || !integer(value.offset) || value.offset > 10000 || typeof value.has_more !== 'boolean'
        || value.has_more !== (value.offset+value.limit < value.total)
        || value.items.length !== Math.max(0, Math.min(value.limit, value.total-value.offset))) fail();
    const keys = new Set();
    for (const row of value.items) { validateRecoveryRequest(row); if (row.execution.enabled !== value.execution_enabled) fail(); if (keys.has(row.id)) fail(); keys.add(row.id); }
  } else {
    validateRecoveryRequest(value.request);
  if (value.request.execution.enabled !== value.execution_enabled) fail();
    if (!Array.isArray(value.events) || value.events.length > 100) fail();
    let previous = 0;
    for (const event of value.events) {
      if (!integer(event.id) || event.id <= previous || !['created', 'approved', 'rejected', 'cancelled'].includes(event.action)
          || !actor(event.by) || !date(event.at) || !integer(event.revision) || event.revision < 1
          || event.revision > value.request.revision || !event.evidence || typeof event.evidence !== 'object') fail();
      previous = event.id;
    }
  }
  if (value.execution_events !== undefined) {
    if (!Array.isArray(value.execution_events) || value.execution_events.length > 100) fail();
    let previous = 0;
    for (const event of value.execution_events) {
      if (!integer(event.id) || event.id <= previous || !['queued','started','blocked','finished','cancelled','interrupted'].includes(event.action)
          || !date(event.at) || !(event.by?.kind === 'human' ? actor(event.by) : event.by?.kind === 'worker' && event.by.id === null && event.by.username === 'worker')
          || !event.evidence || typeof event.evidence !== 'object') fail();
      previous = event.id;
    }
  }
  return value;
}

export function validateRecoveryMutation(value) {
  validateEnvelope(value);
  validateRecoveryRequest(value.request);
  if (value.request.execution.enabled !== value.execution_enabled) fail();
  return value;
}

export function canManageRecovery(auth) {
  return auth?.enabled === true && auth.user?.active === true && auth.user.role === 'admin';
}

export function approvalCurrent(row, now) {
  return row?.status === 'approved' && row.decision.approval_valid === true && Number.isFinite(now)
    && now >= Date.parse(row.decision.at) && now < Date.parse(row.decision.valid_until);
}

export function requestKey(crypto = globalThis.crypto) {
  if (!crypto?.getRandomValues) throw new Error('La création de demandes exige un navigateur récent.');
  return [...crypto.getRandomValues(new Uint8Array(16))].map((v) => v.toString(16).padStart(2, '0')).join('');
}

export function requestPath(identifier) {
  if (!id(identifier)) throw new Error('Demande invalide.');
  return '/api/v1/recovery/requests/' + identifier;
}

export const executionLabels = {not_dispatched:'Non lancé',queued:'En attente de démarrage',running:'Test en cours',
  succeeded:'Restauration vérifiée',failed:'Test échoué',blocked:'Démarrage bloqué',inconclusive:'Résultat non concluant',cancelled:'Test annulé'};
function validateEnvelope(value) {
  if (value?.status !== 'available' || typeof value.execution_enabled !== 'boolean' || !date(value.evaluated_at)
      || value.mode !== (value.execution_enabled ? 'isolated_test' : 'approval_only')) fail();
}
function validateExecution(value) {
  if (!value || typeof value.enabled !== 'boolean' || !Object.hasOwn(executionLabels,value.status)) fail();
  if (value.status === 'not_dispatched') return;
  if (!value.enabled || !id(value.id) || !date(value.queued_at)) fail();
  const terminal = !['queued','running'].includes(value.status);
  if ((value.started_at !== null && (!date(value.started_at) || Date.parse(value.started_at)<Date.parse(value.queued_at)))
      || (terminal ? !date(value.finished_at) || Date.parse(value.finished_at)<Date.parse(value.started_at || value.queued_at) : value.finished_at !== null)
      || (value.status === 'running' && value.started_at === null)
      || (value.reason_code !== null && !/^[a-z][a-z0-9_]{0,95}$/.test(value.reason_code))) fail();
  if (value.report !== null) {
    const report=value.report;
    if (!terminal || report.production_changed !== false || !['restore_validated','failed','blocked','inconclusive'].includes(report.status)
        || !report.cleanup || Object.keys(report.cleanup).length !== 3
        || ['containers_removed','networks_removed','temporary_files_removed'].some((key)=>typeof report.cleanup[key] !== 'boolean')) fail();
    for (const key of ['files_verified','tables_verified','rows_verified','courses','users','quizzes']) {
      if (key in report && (!integer(report[key]) || report[key]>1e12)) fail();
    }
    for (const key of ['database_check','login_check','sequence_check','archive_check']) if (key in report && report[key] !== 'passed') fail();
    if (report.status === 'restore_validated' && (value.status !== 'succeeded' || !Object.values(report.cleanup).every(Boolean) || report.database_check !== 'passed')) fail();
  }
  if (value.status === 'succeeded' && value.report?.status !== 'restore_validated') fail();
}
export function canStartTest(row, now) {
  return row.execution?.enabled === true && row.execution.status === 'not_dispatched' && approvalCurrent(row,now);
}
export function canCancelRequest(row) {
  return ['pending','approved'].includes(row.status) && ['not_dispatched','queued'].includes(row.execution.status);
}
