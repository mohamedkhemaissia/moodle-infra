import { apiFetch } from './authClient.js';
import { validateDocument } from './operations.js';
import { executionLabels, requestLabels } from './recoveryRequests.js';

export const PAGE_SIZE = 50;
export const REPORTS = {
  incidents: { title: 'Incidents', validation: 'incidents',
    note: 'État des incidents à la date de lecture du journal.' },
  diagnostics: { title: 'Diagnostics', validation: 'runs',
    note: 'Une exécution terminée indique que les vérifications ont été exécutées. Elle ne prouve pas la santé du service.' },
  remediations: { title: 'Remédiations', validation: 'runs',
    note: 'Une exécution terminée ne prouve pas à elle seule le rétablissement. Les contrôles fonctionnels sont consultables dans le détail de l’opération.' },
  recovery: { title: 'Tests de restauration', validation: 'recovery_requests',
    note: 'Approbation et résultat du test sont distincts. Une restauration vérifiée concerne l’environnement isolé associé à la demande.' },
};
const states = { open: 'Ouvert', resolved: 'Résolu', completed: 'Exécution terminée',
  blocked: 'Bloqué', failed: 'Échec', running: 'En cours', timeout: 'Délai dépassé',
  canceled: 'Annulé', interrupted: 'Interrompu', unknown: 'Inconnu', clear: 'Normal', triggered: 'Déclenché' };
const invalid = () => { throw new Error('Le journal ne permet pas de produire un rapport fiable.'); };
const integer = (value) => Number.isSafeInteger(value) && value >= 0;

function text(value) {
  if (value === null || value === undefined) return '';
  if (typeof value !== 'string' || value.length > 2000 || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(value)) invalid();
  return value;
}
function timestamp(value, numeric = false) {
  if (value === null || value === undefined) return '';
  if (numeric ? typeof value !== 'number' || !Number.isFinite(value) || value < 0 : typeof value !== 'string') invalid();
  const time = numeric ? value * 1000 : Date.parse(value);
  if (!Number.isFinite(time) || Math.abs(time) > 8.64e15) invalid();
  return new Date(time).toISOString();
}
function count(value) {
  if (value === null || value === undefined) return '';
  if (!integer(value)) invalid();
  return value;
}
function returnCode(value) {
  if (value === null || value === undefined) return '';
  if (!Number.isSafeInteger(value)) invalid();
  return value;
}
function flag(value) {
  if (value === null || value === undefined) return '';
  if (typeof value !== 'boolean') invalid();
  return value ? 'Oui' : 'Non';
}

export function reportPath(kind, { offset = 0, status = '' } = {}) {
  if (!Object.hasOwn(REPORTS, kind) || !integer(offset) || offset % PAGE_SIZE !== 0 ||
      offset > (kind === 'recovery' ? 10000 : 100000) ||
      !['', 'open', 'resolved'].includes(status) || (kind !== 'incidents' && status !== '')) invalid();
  const endpoint = kind === 'recovery' ? 'recovery/requests' : kind;
  return `/api/v1/${endpoint}?limit=${PAGE_SIZE}&offset=${offset}${status ? '&status=' + status : ''}`;
}

function columns(kind) {
  if (kind === 'incidents') return ['Identifiant', 'Titre', 'Signal', 'Catégorie', 'Sévérité', 'État',
    'Dernier état observé', 'Première observation (UTC)', 'Ouverture (UTC)', 'Dernière observation (UTC)', 'Résolution (UTC)'];
  if (kind !== 'recovery') return ['Identifiant', 'Incident', 'Action', 'État', 'Début (UTC)', 'Fin (UTC)', 'État Runner', 'Code Runner'];
  return ['Identifiant', 'Périmètre', 'Point de sauvegarde', 'Demandeur', 'Motif', 'Création (UTC)',
    'Décision', 'Décision par', 'Décision le (UTC)', 'Résultat du test', 'Identifiant du test',
    'Début du test (UTC)', 'Fin du test (UTC)', 'Motif du résultat', 'Production modifiée',
    'Fichiers vérifiés', 'Tables vérifiées', 'Lignes vérifiées', 'Conteneurs supprimés',
    'Réseaux supprimés', 'Fichiers temporaires supprimés'];
}

function project(kind, item) {
  if (kind === 'incidents') return [item.id, text(item.title), text(item.signal_key), text(item.category),
    text(item.severity), states[item.status], Object.hasOwn(states, item.latest_state) ? states[item.latest_state] : text(item.latest_state),
    timestamp(item.first_observed_at), timestamp(item.opened_at), timestamp(item.last_observed_at), timestamp(item.resolved_at)];
  if (kind !== 'recovery') return [item.id, item.incident_id, text(item.action_id), states[item.status],
    timestamp(item.started_at, true), timestamp(item.finished_at, true), text(item.runner_status), returnCode(item.runner_rc)];
  const execution = item.execution;
  const report = execution.report;
  return [item.id, item.scope === 'moodle' ? 'Moodle' : 'Pilotage PostgreSQL', item.snapshot_id,
    item.requested_by.username, item.reason, timestamp(item.created_at), requestLabels[item.status],
    item.decision?.by.username || '', timestamp(item.decision?.at), executionLabels[execution.status],
    execution.id || '', timestamp(execution.started_at), timestamp(execution.finished_at), text(execution.reason_code),
    flag(report?.production_changed), count(report?.files_verified), count(report?.tables_verified), count(report?.rows_verified),
    flag(report?.cleanup?.containers_removed), flag(report?.cleanup?.networks_removed), flag(report?.cleanup?.temporary_files_removed)];
}

export function makeReport(kind, value, { offset = 0, status = '', retrievedAt = Date.now() } = {}) {
  const source = reportPath(kind, { offset, status });
  validateDocument(REPORTS[kind].validation, value);
  if (value.limit !== PAGE_SIZE || value.offset !== offset || value.items.length > PAGE_SIZE ||
      value.items.length !== Math.max(0, Math.min(PAGE_SIZE, value.total - offset)) ||
      value.has_more !== (offset + value.items.length < value.total)) invalid();
  const identifiers = new Set();
  for (const item of value.items) {
    if (identifiers.has(item.id) || (kind === 'incidents' && status && item.status !== status)) invalid();
    identifiers.add(item.id);
  }
  return { kind, title: REPORTS[kind].title, note: REPORTS[kind].note, source,
    retrieved_at: timestamp(retrievedAt / 1000, true), source_evaluated_at: timestamp(value.evaluated_at),
    offset, limit: PAGE_SIZE, total: value.total, has_more: value.has_more,
    filter: status === 'open' ? 'Ouverts' : status === 'resolved' ? 'Résolus' : 'Tous',
    columns: columns(kind), rows: value.items.map((item) => project(kind, item)) };
}

export async function loadReport(kind, options = {}, fetcher) {
  const { signal, offset = 0, status = '' } = options;
  const response = await apiFetch(reportPath(kind, { offset, status }), { method: 'GET', signal,
    headers: { Accept: 'application/json' } }, fetcher);
  if (!response.ok) throw new Error(`Journal indisponible (HTTP ${response.status}).`);
  const value = await response.json();
  return makeReport(kind, value, { offset, status, retrievedAt: Date.now() });
}

function metadata(report, exportedAt) {
  return [['Journal', report.title], ['Lecture du journal (UTC)', report.retrieved_at],
    ['Export (UTC)', timestamp(exportedAt / 1000, true)], ['Source', report.source],
    ['Date fournie par la source (UTC)', report.source_evaluated_at], ['Filtre', report.filter],
    ['Page', report.offset / PAGE_SIZE + 1], ['Total à la lecture', report.total],
    ['Lignes exportées', report.rows.length], ['Autres pages', report.offset > 0 || report.has_more ? 'Oui' : 'Non']];
}

export function csvCell(value) {
  let safe = String(value ?? '');
  // Les guillemets CSV ne neutralisent pas une formule de tableur.
  if (/^\s*[=+\-@]/u.test(safe)) safe = "'" + safe;
  return '"' + safe.replaceAll('"', '""') + '"';
}

export function reportCsv(report, exportedAt = Date.now()) {
  const entries = metadata(report, exportedAt);
  const prefix = entries.map(([, value]) => value);
  const rows = [[...entries.map(([key]) => key), ...report.columns],
    ...report.rows.map((row) => [...prefix, ...row])];
  return '\ufeff' + rows.map((row) => row.map(csvCell).join(';')).join('\r\n') + '\r\n';
}

export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (character) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
}

export function reportHtml(report, exportedAt = Date.now()) {
  const info = metadata(report, exportedAt).map(([key, value]) =>
    `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value === '' ? 'Non renseigné' : value)}</dd></div>`).join('');
  const records = report.rows.map((row, index) => `<article><h2>Entrée ${report.offset + index + 1}</h2><dl>` +
    report.columns.map((column, position) => `<div><dt>${escapeHtml(column)}</dt><dd>${escapeHtml(row[position] === '' ?
      'Non renseigné' : row[position])}</dd></div>`).join('') + '</dl></article>').join('');
  return `<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>${escapeHtml(report.title)} · Pilotage Moodle</title>
<style>body{margin:0;background:#eef2f7;color:#152338;font:15px/1.5 system-ui,sans-serif}main{max-width:960px;margin:32px auto;padding:32px;background:white;border-radius:8px}header{border-bottom:3px solid #2563a6;padding-bottom:20px}h1{margin:4px 0 12px;font-size:28px}h2{font-size:17px;color:#2563a6}p{max-width:78ch}dl{margin:0}dl>div{display:grid;grid-template-columns:minmax(140px,30%) 1fr;gap:12px;padding:8px 0;border-bottom:1px solid #dce2eb}dt{color:#53627a}dd{margin:0;overflow-wrap:anywhere;white-space:pre-wrap}article{margin-top:24px;padding-top:4px}footer{margin-top:32px;color:#53627a;font-size:13px}@media(max-width:640px){main{margin:0;padding:20px}dl>div{grid-template-columns:1fr;gap:2px}}@media print{body{background:white;font-size:11px}main{margin:0;padding:0;max-width:none}article{break-inside:avoid}header{break-inside:avoid}h2{break-after:avoid}}</style></head>
<body><main><header><p>BeeCoders · Pilotage Moodle</p><h1>${escapeHtml(report.title)}</h1>
<p>Capture du journal. Seule la page sélectionnée est incluse dans ce rapport.</p><p>${escapeHtml(report.note)}</p></header>
<section aria-label="Périmètre du rapport"><h2>Périmètre et dates</h2><dl>${info}</dl></section>
${records || '<p>Aucune entrée sur cette page.</p>'}
<footer>Les valeurs absentes sont indiquées « Non renseigné ». Les opérations et résultats conservent leur état enregistré à la date de lecture.</footer>
</main></body></html>`;
}

export function downloadReport(report, format, exportedAt = Date.now()) {
  if (!['csv', 'html'].includes(format) || !Object.hasOwn(REPORTS, report.kind)) invalid();
  const content = format === 'csv' ? reportCsv(report, exportedAt) : reportHtml(report, exportedAt);
  const blob = new Blob([content], { type: format === 'csv' ? 'text/csv;charset=utf-8' : 'text/html;charset=utf-8' });
  const reference = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = reference;
  link.download = `moodle-${report.kind}-page-${report.offset / PAGE_SIZE + 1}-${new Date(exportedAt).toISOString().replace(/[:.]/g, '-')}.${format}`;
  document.body.append(link);
  try { link.click(); } finally { link.remove(); setTimeout(() => URL.revokeObjectURL(reference), 1000); }
}
