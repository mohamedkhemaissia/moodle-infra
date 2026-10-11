import { jsonRequest } from './authClient.js';
import { reportCsv, reportHtml } from './reports.js';

export const HISTORY_SIZE = 50;
export const decisionLabels = { recommendation: 'Recommandation', blocked: 'Bloquée', monitoring: 'Surveillance', closed: 'Clôturée' };
const invalid = () => { throw new Error('Le journal des décisions est invalide.'); };
const positive = (value) => Number.isSafeInteger(value) && value > 0;
const date = (value) => typeof value === 'string' && Number.isFinite(Date.parse(value));
const text = (value) => typeof value === 'string' && value.length <= 32768;
const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

export function validateHistoryEntry(entry) {
  const decision = entry?.decision;
  if (!positive(entry?.id) || !positive(entry.incident_id) || !Object.hasOwn(decisionLabels, entry.status) ||
      entry.persisted !== true || !date(entry.first_observed_at) || !date(entry.last_observed_at) ||
      Date.parse(entry.last_observed_at) < Date.parse(entry.first_observed_at) || !positive(entry.observations) ||
      !object(decision) || decision.incident_id !== entry.incident_id || decision.status !== entry.status ||
      !text(decision.signal_key) || !text(decision.context) || !text(decision.explanation) || !date(decision.input_evaluated_at) ||
      !text(decision.policy_version) || !/^[a-f0-9]{64}$/.test(decision.policy_sha256) ||
      !/^[a-f0-9]{64}$/.test(decision.decision_id) || decision.execution_allowed !== false ||
      !Array.isArray(decision.reason_codes) || decision.reason_codes.some((value) => !text(value)) ||
      !object(decision.evidence) || !object(entry.policy) || entry.policy.sha256 !== decision.policy_sha256 ||
      !object(entry.collector) || (decision.recommended_action !== null &&
        (!object(decision.recommended_action) || !text(decision.recommended_action.title) ||
         decision.recommended_action.executable !== false || !Array.isArray(decision.recommended_action.steps) ||
         decision.recommended_action.steps.some((value) => !text(value))))) invalid();
  return entry;
}

export function historyPath({ status = '', beforeId = null, incidentId = '' } = {}) {
  if ((status && !Object.hasOwn(decisionLabels, status)) || (beforeId !== null && !positive(beforeId)) ||
      (incidentId !== '' && (!/^[1-9][0-9]*$/.test(String(incidentId)) || !positive(Number(incidentId))))) invalid();
  return `/api/v1/decisions/history?limit=${HISTORY_SIZE}${status ? '&status=' + status : ''}` +
    `${beforeId === null ? '' : '&before_id=' + beforeId}${incidentId === '' ? '' : '&incident_id=' + incidentId}`;
}

export function validateHistory(value, options = {}) {
  historyPath(options);
  if (!Array.isArray(value?.items) || value.items.length > HISTORY_SIZE || typeof value.has_more !== 'boolean' ||
      value.execution_allowed !== false || !object(value.coverage) || !date(value.coverage.started_at) ||
      !['waiting', 'running', 'stale', 'clock_inconsistent'].includes(value.coverage.status) ||
      value.coverage.backfilled !== false ||
      (value.coverage.last_input_at !== null && !date(value.coverage.last_input_at)) ||
      (value.coverage.last_evaluated_at !== null && !date(value.coverage.last_evaluated_at))) invalid();
  let previous = options.beforeId ?? Infinity;
  for (const entry of value.items) {
    validateHistoryEntry(entry);
    if (entry.id >= previous || (options.status && entry.status !== options.status) ||
        (options.incidentId && entry.incident_id !== Number(options.incidentId))) invalid();
    previous = entry.id;
  }
  if (value.has_more ? value.items.length !== HISTORY_SIZE || value.next_before_id !== previous : value.next_before_id !== null) invalid();
  return value;
}

export async function loadHistory(options, signal) {
  return validateHistory(await jsonRequest(historyPath(options), { signal }), options);
}

export function makeHistoryReport(value, options = {}, page = 0, retrievedAt = Date.now()) {
  validateHistory(value, options);
  return { kind: 'decision_history', title: 'Historique des décisions',
    note: 'Mesures de la première évaluation de chaque transition. Les confirmations ne remplacent pas ces preuves. ' +
      `Journal activé le ${value.coverage.started_at}. Aucun historique reconstruit avant cette date. Une décision archivée ne permet aucune exécution.`,
    source: historyPath(options), retrieved_at: new Date(retrievedAt).toISOString(),
    source_evaluated_at: value.coverage.last_evaluated_at ?? '', offset: page * HISTORY_SIZE,
    limit: HISTORY_SIZE, total: 'Non compté (pagination par identifiant)', has_more: value.has_more,
    filter: (decisionLabels[options.status] || 'Tous') + (options.incidentId ? ` · Incident ${options.incidentId}` : ''),
    columns: ['Entrée', 'Incident', 'Signal', 'État', 'Première évaluation (UTC)', 'Dernière confirmation (UTC)',
      'Confirmations', 'Justification', 'Motifs', 'Action proposée', 'Mesures (JSON)', 'Contexte / modèle',
      'Version de politique', 'Empreinte de politique', 'Identifiant de décision', 'Politique (JSON)', 'Observateur (JSON)'],
    rows: value.items.map((entry) => [entry.id, entry.incident_id, entry.decision.signal_key, decisionLabels[entry.status],
      entry.first_observed_at, entry.last_observed_at, entry.observations, entry.decision.explanation,
      entry.decision.reason_codes.join(', '), entry.decision.recommended_action?.title || '',
      JSON.stringify(entry.decision.evidence), entry.decision.context, entry.decision.policy_version,
      entry.decision.policy_sha256, entry.decision.decision_id, JSON.stringify(entry.policy), JSON.stringify(entry.collector)]) };
}

export function downloadHistory(report, format) {
  if (!['csv', 'html'].includes(format)) invalid();
  const content = format === 'csv' ? reportCsv(report) : reportHtml(report);
  const url = URL.createObjectURL(new Blob([content], { type: format === 'csv' ? 'text/csv;charset=utf-8' : 'text/html;charset=utf-8' }));
  try {
    const anchor = document.createElement('a');
    anchor.href = url; anchor.download = `decisions-page-${report.offset / HISTORY_SIZE + 1}.${format}`;
    anchor.click();
  } finally { setTimeout(() => URL.revokeObjectURL(url), 1000); }
}
