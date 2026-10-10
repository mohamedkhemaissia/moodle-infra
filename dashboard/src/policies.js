import { jsonRequest } from './authClient.js';

export const MODE_NAMES = { observation: 'Observation', assisted: 'Assisté', automatic: 'Automatique' };
export const ACTION_NAMES = { policy_updated: 'Politique modifiée', action_approved: 'Intervention approuvée', action_authorized: 'Intervention autorisée' };

export function validatePolicy(value) {
  if (!value || !Number.isSafeInteger(value.revision) || value.revision < 1 || value.revision >= 2147483647
      || !Object.hasOwn(MODE_NAMES, value.mode) || typeof value.suspended !== 'boolean'
      || typeof value.evaluation_context !== 'boolean' || !/^[0-9a-f]{64}$/.test(value.sha256 ?? '')
      || typeof value.updated_by !== 'string' || typeof value.reason !== 'string'
      || typeof value.updated_at !== 'string' || !Number.isFinite(Date.parse(value.updated_at))) {
    throw new Error('Politique reçue invalide.');
  }
  return value;
}

export function policyPayload(policy, form) {
  validatePolicy(policy);
  if (!Object.hasOwn(MODE_NAMES, form.mode) || typeof form.suspended !== 'boolean'
      || typeof form.evaluation_context !== 'boolean' || typeof form.confirm_automatic !== 'boolean'
      || typeof form.reason !== 'string' || form.reason.trim().length < 15 || form.reason.trim().length > 500
      || /[\x00-\x1f\x7f]/.test(form.reason)) throw new Error('Renseignez un motif de 15 à 500 caractères.');
  if (form.mode === 'automatic' && !form.confirm_automatic) throw new Error('Confirmez le mode automatique.');
  return { revision: policy.revision, mode: form.mode, suspended: form.suspended,
    evaluation_context: form.evaluation_context, reason: form.reason.trim(), confirm_automatic: form.confirm_automatic };
}

export function approvalPayload(policy, incident, reason, clientKey) {
  validatePolicy(policy);
  const incidentId = typeof incident === 'number' ? incident : /^\d+$/.test(incident) ? Number(incident) : NaN;
  if (!policy.evaluation_context || policy.suspended || policy.mode === 'observation') throw new Error('La politique ne permet pas cette approbation.');
  if (!Number.isSafeInteger(incidentId) || incidentId < 1 || typeof reason !== 'string'
      || reason.trim().length < 15 || reason.trim().length > 500 || /[\x00-\x1f\x7f]/.test(reason)
      || !/^[0-9a-f]{32}$/.test(clientKey)) throw new Error('Vérifiez l’incident et le motif.');
  return { revision: policy.revision, incident_id: incidentId, reason: reason.trim(), client_key: clientKey };
}

export async function loadPolicies(signal, fetcher) {
  const [data, events] = await Promise.all([
    jsonRequest('/api/v1/policies', { signal }, fetcher),
    jsonRequest('/api/v1/policies/events?limit=10', { signal }, fetcher),
  ]);
  if (data?.status !== 'available' || events?.status !== 'available' || !Array.isArray(events.items)
      || !Number.isSafeInteger(events.total) || events.total < events.items.length || events.items.length > 10
      || !['unknown', 'stale', 'idle', 'running', 'error'].includes(data.worker?.status)) throw new Error('Historique des politiques invalide.');
  for (const row of events.items) {
    if (!Number.isSafeInteger(row.id) || row.id < 1 || !Object.hasOwn(ACTION_NAMES, row.action)
        || typeof row.actor !== 'string' || !Number.isSafeInteger(row.revision) || row.revision < 1
        || !Number.isFinite(Date.parse(row.at)) || !row.details || typeof row.details !== 'object') throw new Error('Événement de politique invalide.');
  }
  return { ...data, policy: validatePolicy(data.policy), events: events.items, eventTotal: events.total };
}
