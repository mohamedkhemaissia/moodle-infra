import { useReadings } from './useReadings.js';
import { applicationView } from './applicationMetrics.js';
import './backups.css';

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
  const state = view.collection !== 'up' || view.reachable === null ? 'État inconnu' :
    view.reachable ? 'Accès confirmé' : 'Accès non confirmé';
  return <article className="backup-area">
    <div className="backup-area-heading"><h3>{label}</h3>
      <span className={'ops-badge ' + (view.collection === 'up' && view.reachable === true ? 'good' : 'neutral')}>{state}</span></div>
    {reading.pending ? <p className="ops-empty" role="status">Lecture en cours…</p> :
      reading.error || reading.stale ? <p className="ops-empty" role="status">{reading.error || 'La lecture des services est ancienne.'}</p> :
      <><p className="ops-caption">Collecte : {view.collection === 'up' ? 'récente' : view.collection === 'down' ? 'interrompue' : 'indisponible ou ancienne'}.</p>
        <dl className="backup-grid">{fields[name].map(([key, title, hint]) => <div key={key}>
          <dt>{title}</dt><dd>{view.metrics[key] === null ? '—' : format.format(view.metrics[key])}</dd>
          {hint && <small>{hint}</small>}</div>)}</dl></>}
  </article>;
}

export default function ApplicationServices({ now, refreshToken }) {
  const readings = useReadings(requests, refreshToken, now);
  return <section id="applications" className="panel ops-panel" aria-labelledby="applications-heading">
    <div className="panel-heading"><div><h2 id="applications-heading">Services Moodle</h2>
      <p>Activité du pool PHP et de la base de données.</p></div></div>
    <div className="ops-body"><ApplicationArea name="php_fpm" reading={readings.applications} now={now} />
      <ApplicationArea name="postgresql" reading={readings.applications} now={now} /></div>
  </section>;
}
