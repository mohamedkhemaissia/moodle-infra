import { applicationView } from './applicationMetrics.js';
import { backupView } from './backups.js';
import { catalogCurrent, catalogPath } from './backupCatalog.js';
import { containerView } from './containerMetrics.js';
import { decisionCurrent, requestPath } from './operations.js';

export const NAVIGATION = [
  { id: 'main', title: 'Vue d’ensemble', description: 'Disponibilité Moodle, incidents, ressources et sauvegardes.', icon: 'grid', color: '#0090e7' },
  { id: 'applications', title: 'Services Moodle', description: 'Disponibilité de Moodle, de PHP-FPM et de PostgreSQL.', icon: 'server', color: '#0090e7' },
  { id: 'resources', title: 'Ressources', description: 'Mesures actuelles et état de la collecte de l’hôte Moodle.', icon: 'cpu', color: '#0090e7' },
  { id: 'containers', title: 'Conteneurs', description: 'État et consommation des conteneurs Moodle essentiels.', icon: 'cpu', color: '#8f5fe8' },
  { id: 'analysis', title: 'Analyse IA', description: 'Détection d’anomalies et prévisions des ressources.', icon: 'chart', color: '#00d25b' },
  { id: 'incidents', title: 'Incidents', description: 'Épisodes détectés et recommandations de correction.', icon: 'alert', color: '#0090e7' },
  { id: 'audits', title: 'Historique des exécutions', description: 'Journaux des diagnostics et des remédiations.', icon: 'clock', color: '#0090e7' },
  { id: 'decision-history', title: 'Décisions et recommandations', description: 'Historique des décisions, mesures et justifications.', icon: 'clock', color: '#0090e7' },
  { id: 'backups', title: 'Sauvegardes', description: 'Suivi des sauvegardes et des points de reprise.', icon: 'disk', color: '#8f5fe8' },
  { id: 'policies', title: 'Politiques', description: 'Règles qui encadrent les corrections.', icon: 'server', color: '#0090e7' },
  { id: 'php-profiles', title: 'Profils PHP-FPM', description: 'Configuration des profils de capacité PHP-FPM.', icon: 'server', color: '#00d25b' },
  { id: 'reports', title: 'Rapports', description: 'Consultation et export des opérations enregistrées.', icon: 'chart', color: '#0090e7' },
];

export const NAVIGATION_GROUPS = [
  { id: 'supervision', title: 'Supervision', icon: 'server', sections: ['applications', 'resources', 'containers', 'analysis'] },
  { id: 'actions', title: 'Incidents et actions', icon: 'alert', sections: ['incidents', 'decision-history', 'audits'] },
  { id: 'administration', title: 'Administration', icon: 'server', sections: ['policies', 'php-profiles', 'reports', 'accounts'] },
];

export function sectionFromHash(hash, accountsEnabled = false) {
  const id = typeof hash === 'string' ? hash.replace(/^#/, '') : '';
  return NAVIGATION.some((item) => item.id === id) || (id === 'accounts' && accountsEnabled) ? id : 'main';
}

// Shared labels and operational summaries used by navigation destinations.
const dates = new Intl.DateTimeFormat('fr-FR', { dateStyle: 'short', timeStyle: 'short' });

export const STATUS = {
  operational: ['Opérationnel', 'good'], degraded: ['Dégradé', 'warning'],
  warning: ['Avertissement', 'warning'], critical: ['Critique', 'bad'],
  unavailable: ['Indisponible', 'bad'], stale: ['Données anciennes', 'warning'], unknown: ['Inconnu', 'neutral'],
  open: ['Ouvert', 'warning'], resolved: ['Résolu', 'good'], info: ['Information', 'neutral'],
  completed: ['Terminé', 'neutral'], blocked: ['Bloqué', 'warning'], failed: ['Échec', 'bad'],
  running: ['En cours', 'neutral'], timeout: ['Délai dépassé', 'bad'], canceled: ['Annulé', 'neutral'],
  interrupted: ['Interrompu', 'warning'], recommendation: ['Recommandation', 'warning'],
  monitoring: ['Sous observation', 'neutral'], closed: ['Incident résolu', 'good'],
};

export function displayDate(value, missing = '—') {
  const time = typeof value === 'number' ? value * 1000 : Date.parse(value);
  return Number.isFinite(time) && Math.abs(time) <= 8.64e15 ? dates.format(time) : missing;
}

export function readingStatus(reading) {
  return reading?.stale ? 'stale' : 'unknown';
}

// Existing read-only contracts, shared with the detailed screens.
export const OVERVIEW_REQUESTS = [
  { key: 'summary', kind: 'summary', path: requestPath('summary') },
  { key: 'incidents', kind: 'incidents', path: requestPath('incidents', { status: 'open' }) },
  { key: 'applications', kind: 'applications', path: '/api/v1/metrics/applications' },
  { key: 'containers', kind: 'containers', path: '/api/v1/metrics/containers' },
  { key: 'backup', kind: 'backup', path: '/api/v1/backups/summary' },
  { key: 'catalog', kind: 'backup_catalog', path: catalogPath('moodle') },
];

export function observerCurrent(document, now) {
  const collector = document?.collector;
  const delta = now - Date.parse(document?.evaluated_at);
  return collector?.status === 'running' && Number.isFinite(delta) && delta >= -30_000 && delta <= 60_000 &&
    [collector.age_seconds, collector.input_age_seconds].every((age) =>
      typeof age === 'number' && Number.isFinite(age) && age >= 0 && age + Math.max(0, delta / 1000) <= 90);
}

export function importantIncidents(reading) {
  const priorities = { critical: 3, warning: 2, info: 1 };
  return (reading?.data?.items || []).filter((row) => row.status === 'open')
    .slice().sort((a, b) => (priorities[b.severity] || 0) - (priorities[a.severity] || 0) || b.id - a.id).slice(0, 3);
}

export function currentRecommendations(incidents, decisions, summary, now) {
  if (!observerCurrent(summary, now)) return [];
  return incidents.flatMap((incident) => {
    const value = decisions[incident.id]?.data;
    return value?.decision.incident_id === incident.id && decisionCurrent(value, now) && value.decision.recommended_action
      ? [{ incident, decision: value.decision }] : [];
  });
}

export function moodleStatus(view, readings, now) {
  if (view.metrics.moodle_available === false) return 'unavailable';
  if (view.metrics.moodle_available !== true) return view.reason === 'stale' || view.collection?.moodle_https?.status === 'stale' ? 'stale' : 'unknown';
  const php = applicationView(readings.applications?.data, 'php_fpm', now);
  const database = applicationView(readings.applications?.data, 'postgresql', now);
  const containers = containerView(readings.containers?.data, now);
  return php.reachable === false || database.reachable === false || containers.rows.some((row) =>
    ['apache', 'php', 'postgres'].includes(row.service) && (['missing', 'paused', 'not_running'].includes(row.state) || row.health === 'unhealthy'))
    ? 'degraded' : 'operational';
}

export function moodleBackupStatus(readings, now) {
  const backup = backupView(readings.backup?.data, now);
  const catalog = readings.catalog?.data;
  const current = catalogCurrent(catalog, now);
  const presence = current ? catalog.latest_success_presence : 'unknown';
  // A repository presence does not prove integrity or restoration.
  const associated = current && catalog.latest_success_id === readings.backup?.data?.latest_success?.snapshot;
  const status = backup.reason === 'collection_stale' || readings.backup?.stale || readings.catalog?.stale ? 'stale' :
    backup.state === 'missing' || backup.state === 'overdue' || (associated && presence === 'absent') || readings.backup?.data?.job?.last_result === 'failed'
      ? 'warning' : backup.state === 'recent' && associated && presence === 'present' ? 'operational' : 'unknown';
  return { ...backup, status, presence: associated ? presence : 'unknown' };
}
