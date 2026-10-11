import { useReadings } from './useReadings.js';
import { containerView } from './containerMetrics.js';
import './backups.css';
import './containers.css';

const requests = [{ key: 'containers', kind: 'containers', path: '/api/v1/metrics/containers' }];
const format = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 1 });
const names = { running: 'Démarré', not_running: 'Non démarré', paused: 'En pause',
  missing: 'Absent', ambiguous: 'Plusieurs instances', unknown: 'État inconnu' };
const healthNames = { healthy: 'Contrôle Docker réussi', unhealthy: 'Contrôle Docker en échec', starting: 'Contrôle Docker en cours' };
const serviceNames = { apache: 'Apache', php: 'PHP-FPM', postgres: 'PostgreSQL' };
const number = (value, unit = '') => value === null ? '—' : format.format(value) + unit;

export function ContainerTable({ reading, now }) {
  const view = containerView(reading.data, now);
  return <>
    {reading.pending && <p role="status">Lecture des conteneurs…</p>}
    {reading.error && <p className="catalog-warning" role="status">{reading.error}</p>}
    {!reading.pending && !reading.error && !view.fresh && <p className="catalog-warning" role="status">
      Collecte indisponible ou ancienne. Les états et les ressources sont inconnus.</p>}
    {!reading.pending && !reading.error && view.fresh && view.rows.every((row) => row.state === 'unknown') &&
      <p className="catalog-warning" role="status">Les mesures des conteneurs Moodle ne sont pas encore disponibles.</p>}
    {view.rows.length > 0 && <div className="container-table-scroll"><table className="catalog-table">
      <caption className="catalog-caption">Conteneurs essentiels de Moodle</caption>
      <thead><tr><th scope="col">Service</th><th scope="col">État Docker</th><th scope="col">CPU</th>
        <th scope="col">Mémoire utilisée</th><th scope="col">Limite effective</th><th scope="col">Processus</th></tr></thead>
      <tbody>{view.rows.map((row) => <tr key={`${row.project}/${row.service}`}>
        <th scope="row"><strong>{serviceNames[row.service] || row.service}</strong><small>{row.project}</small></th>
        <td><span className={`badge ${row.state === 'running' ? 'badge-success' : ['unknown', 'ambiguous'].includes(row.state) ? 'badge-muted' : 'badge-warning'}`}>
          {names[row.state]}</span>{row.health && <small>{healthNames[row.health]}</small>}</td>
        <td>{number(row.metrics.cpu_usage_percent, ' %')}</td>
        <td>{number(row.metrics.memory_working_set_bytes === null ? null : row.metrics.memory_working_set_bytes / 1048576, ' Mio')}</td>
        <td>{number(row.metrics.memory_limit_bytes === null ? null : row.metrics.memory_limit_bytes / 1048576, ' Mio')}</td>
        <td>{number(row.metrics.pids)}</td>
      </tr>)}</tbody></table></div>}
    <p className="backup-association">CPU : 100 % correspond à un cœur utilisé. La mémoire exclut le cache inactif.
      Sans limite explicite, la limite effective peut correspondre à la RAM de l’hôte.
      Un conteneur démarré ne garantit pas le fonctionnement de Moodle.</p>
  </>;
}

export default function Containers({ now, refreshToken }) {
  const readings = useReadings(requests, refreshToken, now);
  return <section id="containers" className="panel ops-panel" aria-labelledby="containers-heading">
    <div className="panel-heading"><div><h2 id="containers-heading">Conteneurs</h2>
      <p>Apache, PHP-FPM et PostgreSQL du pilote.</p></div></div>
    <div className="ops-body"><ContainerTable reading={readings.containers} now={now} /></div>
  </section>;
}
