// Presentation only: the API documents and their validation remain unchanged.
const numbers = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 1 });
const dates = new Intl.DateTimeFormat('fr-FR', { dateStyle: 'short', timeStyle: 'medium' });
const first = (...values) => values.find((value) => value !== null && value !== undefined);
const number = (value, unit = '') => typeof value === 'number' && Number.isFinite(value)
  ? numbers.format(value) + unit : null;
const answer = (value) => typeof value === 'boolean' ? value ? 'Réussi' : 'Échec' : null;
const date = (value) => {
  const time = typeof value === 'number' ? value * 1000 : Date.parse(value);
  return Number.isFinite(time) && Math.abs(time) <= 8.64e15 ? dates.format(time) : null;
};
export function SummaryList({ rows }) {
  const visible = rows.filter(([, value]) => value !== null && value !== undefined && value !== '');
  return visible.length ? <dl className="readable-facts">{visible.map(([label, value]) =>
    <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl> : null;
}

export function EvidenceSummary({ evidence = {} }) {
  const e = evidence?.metrics || evidence || {};
  const measured = (key) => e.metric === key ? e.value : null;
  return <SummaryList rows={[
    ['CPU utilisé', number(first(e.cpu_usage_percent, e.cpu, measured('cpu_usage_percent')), ' %')],
    ['Mémoire utilisée', number(first(e.memory_usage_percent, e.memory, measured('memory_usage_percent')), ' %')],
    ['Disque disponible', number(first(e.disk_available_percent, measured('disk_available_percent')), ' %')],
    ['Requêtes en attente', number(e.listen_queue)],
    ['Processus PHP actifs', number(e.active_processes)],
    ['Processus PHP disponibles', number(e.idle_processes)],
    ['Contrôle HTTPS', answer(first(e.probe_success, e.moodle_available))],
    ['Réponse HTTP', number(first(e.http_status_code, e.moodle_http_status_code, e.http_status))],
    ['Temps de réponse', number(first(e.probe_duration_ms, e.moodle_probe_duration_ms), ' ms')],
    ['Score d’anomalie', number(e.score)],
    ['Seuil de détection', number(first(e.trigger_threshold, e.threshold), e.metric ? ' %' : '')],
  ]} />;
}

const actions = {
  start_php_stopped: 'Remise en service de PHP', apply_php_fpm_profile: 'Adaptation du profil PHP-FPM',
  diagnose_moodle_https: 'Diagnostic de disponibilité', diagnose_cpu_usage: 'Diagnostic CPU',
  diagnose_memory_usage: 'Diagnostic mémoire', diagnose_disk_usage: 'Diagnostic disque',
  diagnose_php_pool: 'Diagnostic du pool PHP-FPM', inspect_prometheus: 'Contrôle de la collecte',
  inspect_linux_collection: 'Contrôle des mesures du serveur', inspect_moodle_collection: 'Contrôle de la sonde Moodle',
  wait_contiguous_history: 'Contrôle de l’historique des mesures', inspect_anomaly_model: 'Contrôle du modèle d’anomalies',
  investigate_statistical_anomaly: 'Analyse de l’anomalie détectée',
};
const modes = { automatic: 'Automatique', automatic_guarded: 'Automatique avec contrôles', assisted: 'Assisté',
  manual: 'Manuel', manual_guarded: 'Manuel avec contrôles', observation: 'Observation', read_only: 'Consultation et diagnostic' };
const checkStates = { ok: 'Vérifié', warning: 'À vérifier', unavailable: 'Indisponible' };
const profiles = { normal: 'Normal', burst: 'Renforcé' };
const reasons = {
  https_and_new_probe_confirmed: 'Rétablissement confirmé par HTTPS et de nouvelles mesures.',
  php_profile_and_new_probe_confirmed: 'Changement de profil confirmé par les vérifications.',
  profile_reload_queue_and_https_confirmed: 'Profil PHP appliqué, file d’attente et accès HTTPS vérifiés.',
  incident_resolved: 'L’incident est résolu.', operating_policy_observation: 'Le mode Observation autorise uniquement le suivi.',
  insufficient_resources: 'Les ressources disponibles ne permettent pas cette correction.',
};
export function RunSummary({ run }) {
  const report = run.report || {};
  const checks = report.checks || {};
  const after = checks.metrics_after?.metrics || {};
  const observed = checks.metrics_after?.metrics || checks.current_metrics?.metrics || {};
  const pool = checks.applications_after?.services?.php_fpm?.metrics || checks.application_metrics?.services?.php_fpm?.metrics || {};
  const start = typeof run.started_at === 'number' ? run.started_at * 1000 : Date.parse(run.started_at);
  const end = typeof run.finished_at === 'number' ? run.finished_at * 1000 : Date.parse(run.finished_at);
  return <>
    <SummaryList rows={[
      ['Action', actions[report.action] || actions[report.decision?.correction_action] || actions[run.action_id] || actions[report.decision?.action_id] || null],
      ['Mode', modes[report.mode] || null], ['Début', date(run.started_at)], ['Fin', date(run.finished_at)],
      ['Durée', Number.isFinite(start) && Number.isFinite(end) && end >= start ? number((end - start) / 1000, ' s') : null],
      ['Réponse HTTP avant', number(checks.https_before?.http_status)],
      ['Réponse HTTP après', number(after.moodle_http_status_code)],
      ['Retour de Moodle', answer(after.moodle_available)],
      ['Profil précédent', profiles[report.profile_change?.from]],
      ['Profil appliqué', profiles[report.profile_change?.to]],
      ['Capacité PHP après', number(report.profile_change?.max_children_after, ' processus')],
      ['File d’attente avant', number(checks.pool_before?.listen_queue)],
      ['Retour au profil précédent', answer(report.rollback?.confirmed)],
      ['Collecte des mesures', checkStates[checks.prometheus_targets?.status]],
      ['Historique des mesures', checkStates[checks.linux_history?.status]],
      ['État des services', checkStates[checks.service_states?.status]],
    ]} />
    {reasons[report.reason] && <p className="ops-caption">{reasons[report.reason]}</p>}
    <EvidenceSummary evidence={observed} />
    <EvidenceSummary evidence={pool} />
    {run.events?.some((event) => event.task) && <><h4>Étapes vérifiées</h4>
      <ul className="execution-steps">{run.events.filter((event) => event.task).slice(-5).map((event, index) =>
        <li key={index}><span>{event.task}</span><span className={'ops-badge ' + (event.event === 'runner_on_ok' ? 'good' :
          ['runner_on_failed', 'runner_on_unreachable'].includes(event.event) ? 'bad' : 'neutral')}>
          {event.event === 'runner_on_ok' ? 'Réussi' : ['runner_on_failed', 'runner_on_unreachable'].includes(event.event) ? 'Échec' : 'Enregistré'}</span></li>)}</ul></>}
  </>;
}

export function ForecastMethods({ forecasts }) {
  const methods = { persistence: 'Maintien de la dernière valeur', ridge: 'Régression', linear_trend: 'Tendance récente' };
  return <SummaryList rows={Object.entries(forecasts).map(([horizon, forecast]) => {
    const used = [...new Set(Object.values(forecast.metrics).map((metric) => methods[metric.method] || 'Modèle de prévision'))];
    return ['À ' + horizon + ' minutes', used.join(' · ')];
  })} />;
}
