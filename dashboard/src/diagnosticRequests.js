import { jsonRequest } from "./authClient.js";
import { requestKey } from "./recoveryRequests.js";

export { requestKey };
export const diagnosticStates = { pending: "En attente", running: "En cours", completed: "Termine", blocked: "Bloque", failed: "Echec", interrupted: "Interrompu" };
const validId = (value) => typeof value === "string" && /^[0-9a-f]{32}$/.test(value);
const validRow = (row) => validId(row?.id) && Number.isSafeInteger(row.incident_id) && row.incident_id > 0 && Object.hasOwn(diagnosticStates, row.status);

export async function createDiagnosticRequest({ incidentId, reason, clientKey, csrfToken, signal }) {
  if (!Number.isSafeInteger(incidentId) || incidentId < 1 || typeof reason !== "string" || reason.trim().length < 15 || reason.length > 500 || !validId(clientKey)) throw new Error("Demande de diagnostic invalide.");
  const value = await jsonRequest("/api/v1/diagnostic-requests", { method: "POST", csrfToken, signal, body: { incident_id: incidentId, reason, client_key: clientKey } });
  if (value?.status !== "available" || !validRow(value.request) || value.request.incident_id !== incidentId || typeof value.created !== "boolean") throw new Error("Reponse de creation invalide.");
  return value.request;
}

export async function listDiagnosticRequests(incidentId, signal) {
  if (!Number.isSafeInteger(incidentId) || incidentId < 1) throw new Error("Incident invalide.");
  const value = await jsonRequest(`/api/v1/diagnostic-requests?incident_id=${incidentId}&limit=10&offset=0`, { signal });
  if (value?.status !== "available" || !Array.isArray(value.items) || !Number.isSafeInteger(value.total) || value.total < 0 || value.items.length > 10 || !value.items.every((row) => validRow(row) && row.incident_id === incidentId && typeof row.reason === "string" && typeof row.requested_by === "string" && Number.isFinite(row.created_at))) throw new Error("Liste des diagnostics invalide.");
  return value;
}
