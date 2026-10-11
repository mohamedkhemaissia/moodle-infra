import { useEffect, useId, useRef, useState } from 'react';
import { useReadings } from './useReadings.js';
import { applicationView } from './applicationMetrics.js';
import { containerView } from './containerMetrics.js';
import { requestPath } from './operations.js';
import { STATUS, displayDate, readingStatus, OVERVIEW_REQUESTS, currentRecommendations, importantIncidents, moodleBackupStatus, moodleStatus, observerCurrent } from './navigation.js';
import './backups.css';

// Shared presentation components for operational screens.
export function StatusBadge({ state = 'unknown', label }) {
  const [name, tone] = STATUS[state] || STATUS.unknown;
  return <span className={`ops-badge status-badge ${tone}`}>{label || name}</span>;
}

export function DetailsPanel({ title = 'Afficher les détails', children, className = '', ...props }) {
  return <details className={`details-panel ${className}`} {...props}>
    <summary>{title}</summary><div className="details-content">{children}</div>
  </details>;
}

export function CompactTable({ caption, columns, rows, rowKey, selectedKey, details, className = '' }) {
  const [detailKey, setDetailKey] = useState(null);
  const panelId = useId();
  const heading = useRef(null);
  const trigger = useRef(null);
  const selected = rows.find((row) => rowKey(row) === detailKey);
  useEffect(() => { if (selected) heading.current?.focus(); }, [detailKey, !!selected]);
  const visibleColumns = details ? [...columns, { key: 'details', label: 'Détails', render: (row) =>
    <button className="ops-button" aria-label={'Consulter ' + rowKey(row)} aria-expanded={detailKey === rowKey(row)}
      aria-controls={detailKey === rowKey(row) ? panelId : undefined} onClick={(event) => {
        trigger.current = event.currentTarget; setDetailKey((before) => before === rowKey(row) ? null : rowKey(row));
      }}>Consulter</button> }] : columns;
  return <><div className="compact-table-wrap"><table className={`compact-table ${className}`}>
    <caption className="sr-only">{caption}</caption>
    <thead><tr>{visibleColumns.map((column) => <th scope="col" key={column.key}>{column.label}</th>)}</tr></thead>
    <tbody>{rows.map((row) => { const key = rowKey(row);
      return <tr key={key} className={selectedKey === key || detailKey === key ? 'selected' : undefined}>
        {visibleColumns.map((column, index) => index === 0 ?
          <th scope="row" data-label={column.label} key={column.key}>{column.render(row)}</th> :
          <td data-label={column.label} key={column.key}>{column.render(row)}</td>)}
      </tr>;
    })}</tbody>
  </table></div>
  {details && selected && <section id={panelId} className="table-detail details-content" aria-label={caption + ' : détail'}>
    <div className="overview-heading"><h3 ref={heading} tabIndex={-1}>Détails · {rowKey(selected)}</h3>
      <button className="ops-button" onClick={() => { setDetailKey(null); trigger.current?.focus(); }}>Fermer le détail</button></div>
    {details(selected)}
  </section>}</>;
}

export function MetricCard({ label, value, hint }) {
  const present = typeof value === 'number' && Number.isFinite(value);
  return <article className="card metric-card" data-metric={label}>
    <p className="metric-label">{label}</p>
    <div className={`metric-value ${present ? '' : 'missing'}`}>
      {present ? new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 1 }).format(value) : '—'}
      {present && <span className="unit">%</span>}
    </div>
    <div className="meter" aria-hidden="true"><div style={{ width: `${present ? Math.min(100, Math.max(0, value)) : 0}%` }} /></div>
    <p className="metric-hint">{present ? hint : 'Mesure indisponible'}</p>
  </article>;
}

export function ResourceMetrics({ metrics }) {
  return <div className="metrics-grid" aria-label="Mesures actuelles">
    <MetricCard label="CPU utilisé" value={metrics.cpu_usage_percent} hint="Moyenne sur 2 minutes" />
    <MetricCard label="Mémoire utilisée" value={metrics.memory_usage_percent} hint="Part de la mémoire utilisée" />
    <MetricCard label="Disque disponible" value={metrics.disk_available_percent} hint="Espace libre sur le disque racine" />
  </div>;
}

const requests = [{ key: 'applications', kind: 'applications', path: '/api/v1/metrics/applications' }];
const format = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 2 });
const fields = {
  php_fpm: [['active_processes', 'Processus actifs'], ['idle_processes', 'Processus disponibles'],
    ['total_processes', 'Processus du pool'], ['listen_queue', 'Requêtes en attente'],
    ['max_children_reached', 'Limite du pool atteinte', 'Compteur depuis le démarrage de PHP']],
  postgresql: [['database_connections', 'Connexions Moodle'], ['server_connections', 'Connexions du serveur'],
    ['max_connections', 'Limite de connexions du serveur'], ['commits_per_second', 'Transactions validées', 'Par seconde · moyenne sur 2 minutes'],
    ['rollbacks_per_second', 'Transactions annulées', 'Par seconde · moyenne sur 2 minutes'],
    ['deadlocks_total', 'Blocages mutuels', 'Compteur depuis la remise à zéro des statistiques']],
};

export function ApplicationArea({ name, reading, now }) {
  const view = applicationView(reading.data, name, now);
  const label = name === 'php_fpm' ? 'PHP-FPM' : 'PostgreSQL Moodle';
  const state = view.collection !== 'up' || view.reachable === null ? readingStatus(reading) :
    view.reachable ? 'operational' : 'unavailable';
  return <article className="backup-area">
    <div className="backup-area-heading"><h3>{label}</h3>
      <StatusBadge state={state} /></div>
    {reading.pending ? <p className="ops-empty" role="status">Lecture en cours…</p> :
      reading.error || reading.stale ? <p className="ops-empty" role="status">{reading.error || 'La lecture des services est ancienne.'}</p> :
      <><p className="ops-caption">{view.reachable === true ? 'Accès confirmé. ' : view.reachable === false ? 'Accès non confirmé. ' : ''}
        Collecte : {view.collection === 'up' ? 'récente' : view.collection === 'down' ? 'interrompue' : 'indisponible ou ancienne'}.</p>
        <dl className="backup-grid">{fields[name].map(([key, title, hint]) => <div key={key}>
          <dt>{title}</dt><dd>{view.metrics[key] === null ? '—' : format.format(view.metrics[key])}</dd>
          {hint && <small>{hint}</small>}</div>)}</dl></>}
  </article>;
}

export function ApplicationTable({ reading, view, now, selected, onSelect }) {
  const rows = ['php_fpm', 'postgresql'].map((name) => {
    const state = applicationView(reading.data, name, now);
    return { id: name, name: name === 'php_fpm' ? 'PHP-FPM' : 'PostgreSQL Moodle',
      state: state.reachable === true ? 'operational' : state.reachable === false ? 'unavailable' : readingStatus(reading),
      health: state.collection === 'up' ? 'Collecte récente' : state.collection === 'down' ? 'Collecte interrompue' : 'Collecte à confirmer',
      date: reading.data?.evaluated_at };
  });
  if (view) rows.unshift({ id: 'moodle', name: 'Accès Moodle',
    state: view.metrics.moodle_available === true ? 'operational' : view.metrics.moodle_available === false ? 'unavailable' :
      view.reason === 'stale' || view.collection.moodle_https?.status === 'stale' ? 'stale' : 'unknown',
    health: view.collection.moodle_https?.status === 'up' ? 'Sonde HTTPS récente' : 'Sonde HTTPS à confirmer' });
  const columns = [
    { key: 'name', label: 'Service', render: (row) => row.name },
    { key: 'state', label: 'État', render: (row) => <StatusBadge state={row.state} /> },
    { key: 'health', label: 'Santé', render: (row) => row.health },
    { key: 'date', label: 'Dernière lecture', render: (row) => row.id === 'moodle' ?
      Number.isFinite(view.collection.moodle_https?.age) ? Math.round(view.collection.moodle_https.age) + ' s depuis la mesure' : '—' : displayDate(row.date) },
    { key: 'details', label: 'Détails', render: (row) => <button className="ops-button" aria-label={'Détails de ' + row.name}
      aria-expanded={selected === row.id} onClick={() => onSelect(selected === row.id ? null : row.id)}>Détails</button> },
  ];
  return <CompactTable caption="Santé des services Moodle" columns={columns} rows={rows} rowKey={(row) => row.id} selectedKey={selected} />;
}

export default function ApplicationServices({ now, view, refreshToken }) {
  const readings = useReadings(requests, refreshToken, now);
  const [selected, setSelected] = useState(null);
  return <section id="applications" className="panel ops-panel" aria-labelledby="applications-heading">
    <div className="panel-heading"><div><h2 id="applications-heading">Services Moodle</h2>
      <p>Disponibilité des services et détails à la demande.</p></div></div>
    <div className="ops-body">
      {readings.applications.pending && <p className="ops-empty" role="status">Lecture des services…</p>}
      {readings.applications.error && <p className="catalog-warning" role="status">{readings.applications.error}</p>}
      <ApplicationTable reading={readings.applications} view={view} now={now} selected={selected} onSelect={setSelected} />
      {selected === 'moodle' && <DetailsPanel title="Détails de la sonde HTTPS" open>
        <dl className="summary-facts"><div><dt>Temps de réponse</dt><dd>{view.metrics.moodle_probe_duration_ms === null ? '—' : format.format(view.metrics.moodle_probe_duration_ms) + ' ms'}</dd></div>
          <div><dt>Code HTTP</dt><dd>{view.metrics.moodle_http_status_code ?? '—'}</dd></div></dl>
      </DetailsPanel>}
      {['php_fpm', 'postgresql'].includes(selected) && <div className="details-content service-detail">
        <ApplicationArea name={selected} reading={readings.applications} now={now} /></div>}
    </div>
  </section>;
}

const numbers = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 1 });

function SectionLink({ section, children, onNavigate, incidentId }) {
  return <a className="section-link" href={`#${section}`} onClick={(event) => {
    if (onNavigate) { event.preventDefault(); onNavigate(section, incidentId); }
  }}>{children}<span aria-hidden="true"> →</span></a>;
}

export function OverviewView({ view, readings, decisions = {}, now, onNavigate }) {
  const status = moodleStatus(view, readings, now);
  const observed = observerCurrent(readings.summary?.data, now);
  const incidents = importantIncidents(readings.incidents);
  const count = readings.summary?.data?.counts.open;
  const alertStatus = !observed ? readingStatus(readings.summary) : count > 0 ?
    (incidents.some((row) => row.severity === 'critical') ? 'critical' : 'warning') : 'operational';
  const backup = moodleBackupStatus(readings, now);
  const php = applicationView(readings.applications?.data, 'php_fpm', now);
  const apache = containerView(readings.containers?.data, now).rows.find((row) => row.service === 'apache');
  const recommendations = currentRecommendations(incidents, decisions, readings.summary?.data, now);
  const services = [
    ['Apache', apache?.state === 'running' && (apache.health === 'healthy' || view.metrics.moodle_available === true) ?
      apache.health === 'unhealthy' ? 'degraded' : 'operational' : ['missing', 'paused', 'not_running'].includes(apache?.state) ? 'unavailable' : readingStatus(readings.containers),
    apache?.state === 'running' ? 'Conteneur démarré' : 'État du service à vérifier'],
    ['PHP-FPM', php.reachable === true ? 'operational' : php.reachable === false ? 'unavailable' : readingStatus(readings.applications),
      php.reachable === true ? 'Accès confirmé' : 'Disponibilité non confirmée'],
  ];
  return <section id="overview" className="overview" aria-label="Vue de pilotage Moodle">
    <div className="health-summary card">
      <div><p className="eyebrow">Plateforme Moodle</p><h2>{status === 'operational' ? 'Moodle est accessible' :
        status === 'degraded' ? 'Un service Moodle est dégradé' : status === 'unavailable' ? 'Moodle est indisponible' : 'Disponibilité à confirmer'}</h2>
        <p className="ops-caption">{view.metrics.moodle_probe_duration_ms !== null && view.metrics.moodle_probe_duration_ms !== undefined ?
          `Réponse HTTPS : ${numbers.format(view.metrics.moodle_probe_duration_ms)} ms` : 'En attente de mesures HTTPS récentes.'}</p></div>
      <StatusBadge state={status} />
    </div>
    <div className="overview-grid">
      <article className="card overview-panel" aria-labelledby="overview-alerts">
        <div className="overview-heading"><h2 id="overview-alerts">Alertes et incidents</h2><StatusBadge state={alertStatus} /></div>
        <p className="overview-count">{typeof count === 'number' ? count : '—'} <span>incident{count !== 1 ? 's' : ''} ouvert{count !== 1 ? 's' : ''}</span></p>
        {!observed && <p className="ops-caption" role="status">Le suivi des incidents ne confirme pas l’état actuel.</p>}
        {incidents.length > 0 ? <ul className="overview-list">{incidents.map((row) => <li key={row.id}>
          <div><strong>#{row.id} · {row.title}</strong><small>{displayDate(row.opened_at)}</small></div><StatusBadge state={row.severity} />
        </li>)}</ul> : observed && count === 0 ? <p className="ops-caption">Aucun incident ouvert enregistré.</p> :
          <p className="ops-caption">{readings.incidents?.error || 'Liste des incidents indisponible ou en cours de lecture.'}</p>}
        {incidents.length > 0 && <p className="ops-caption">Priorités parmi les 10 incidents ouverts les plus récents.</p>}
        <SectionLink section="incidents" onNavigate={onNavigate}>Consulter les incidents</SectionLink>
      </article>
      <article className="card overview-panel" aria-labelledby="overview-backups">
        <div className="overview-heading"><h2 id="overview-backups">Sauvegardes Moodle</h2><StatusBadge state={backup.status} /></div>
        <dl className="summary-facts"><div><dt>Dernier point confirmé</dt><dd>{displayDate(readings.backup?.data?.latest_success?.frozen_at_utc)}</dd></div>
          <div><dt>Ancienneté</dt><dd>{Number.isFinite(backup.ageHours) ? numbers.format(backup.ageHours) + ' h' : 'Inconnue'}</dd></div>
          <div><dt>Disponibilité dans le dépôt</dt><dd>{backup.presence === 'present' ? 'Présent' : backup.presence === 'absent' ? 'Absent' : 'À confirmer'}</dd></div></dl>
        {(readings.backup?.error || readings.catalog?.error) && <p className="ops-caption" role="status">Suivi des sauvegardes incomplet.</p>}
        <SectionLink section="backups" onNavigate={onNavigate}>Consulter les sauvegardes</SectionLink>
      </article>
    </div>
    <div className="overview-section-heading"><h2>Ressources principales</h2>
      <SectionLink section="resources" onNavigate={onNavigate}>Voir les ressources</SectionLink></div>
    <ResourceMetrics metrics={view.metrics} />
    <div className="overview-grid">
      <article className="card overview-panel" aria-labelledby="overview-services">
        <div className="overview-heading"><h2 id="overview-services">Services utiles</h2></div>
        <ul className="overview-list service-summary">{services.map(([name, state, hint]) => <li key={name}>
          <div><strong>{name}</strong><small>{hint}</small></div><StatusBadge state={state} /></li>)}</ul>
        <SectionLink section="applications" onNavigate={onNavigate}>Détails des services</SectionLink>
      </article>
      <article className="card overview-panel" aria-labelledby="overview-actions">
        <div className="overview-heading"><h2 id="overview-actions">Actions recommandées</h2></div>
        {recommendations.length ? <ul className="overview-list">{recommendations.map(({ incident, decision }) => <li key={incident.id}>
          <div><strong>{decision.recommended_action.title}</strong><small>Incident #{incident.id}</small>
            <SectionLink section="incidents" incidentId={incident.id} onNavigate={onNavigate}>Consulter la recommandation</SectionLink></div></li>)}</ul> :
          <p className="ops-caption">{observed && count === 0 ? 'Aucune action recommandée pour les incidents ouverts.' :
            'Consultez les incidents pour leurs recommandations, blocages et données actuelles.'}</p>}
        {backup.status === 'warning' && <p className="ops-caption">Vérifier le point de sauvegarde et sa disponibilité dans le dépôt.</p>}
        <SectionLink section="policies" onNavigate={onNavigate}>Consulter la politique active</SectionLink>
      </article>
    </div>
  </section>;
}

export function Overview({ view, now, refreshToken, onNavigate }) {
  const readings = useReadings(OVERVIEW_REQUESTS, refreshToken, now);
  const requests = importantIncidents(readings.incidents).map((incident) => ({ key: String(incident.id), kind: 'decision',
    path: requestPath('decision', { incidentId: incident.id }) }));
  // Separate polling avoids resetting the summary when the incident selection changes.
  const decisions = useReadings(requests, refreshToken, now);
  return <OverviewView view={view} readings={readings} decisions={decisions} now={now} onNavigate={onNavigate} />;
}
