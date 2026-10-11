import { useReadings } from './useReadings.js';
import { containerView } from './containerMetrics.js';
import { CompactTable, DetailsPanel, StatusBadge } from './ApplicationServices.jsx';
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
    {view.rows.length > 0 && <CompactTable caption="Conteneurs essentiels de Moodle" rows={view.rows} rowKey={(row) => `${row.project}/${row.service}`}
      columns={[
        { key: 'name', label: 'Service', render: (row) => <><strong>{serviceNames[row.service] || row.service}</strong><small>{row.project}</small></> },
        { key: 'state', label: 'État', render: (row) => <StatusBadge state={row.state === 'running' ? 'operational' :
          ['unknown', 'ambiguous'].includes(row.state) ? reading.stale ? 'stale' : 'unknown' : 'warning'} label={names[row.state]} /> },
        { key: 'health', label: 'Santé', render: (row) => <StatusBadge state={row.health === 'healthy' ? 'operational' :
          row.health === 'unhealthy' ? 'degraded' : row.health === 'starting' ? 'warning' : 'unknown'} /> },
        { key: 'resources', label: 'Ressources', render: (row) => <>{number(row.metrics.cpu_usage_percent, ' % CPU')}
          <small>{number(row.metrics.memory_working_set_bytes === null ? null : row.metrics.memory_working_set_bytes / 1048576, ' Mio')}</small></> },
      ]} details={(row) => <dl><dt>Projet / service</dt><dd>{row.project} / {row.service}</dd>
            <dt>Limite mémoire effective</dt><dd>{number(row.metrics.memory_limit_bytes === null ? null : row.metrics.memory_limit_bytes / 1048576, ' Mio')}</dd>
            <dt>Processus</dt><dd>{number(row.metrics.pids)}</dd><dt>Instances</dt><dd>{view.fresh ? row.instances ?? '—' : '—'}</dd>
            <dt>Contrôle Docker</dt><dd>{healthNames[row.health] || 'Aucun contrôle confirmé'}</dd>
            <dt>Âge des statistiques à la lecture</dt><dd>{number(row.stats_age_seconds, ' s')}</dd></dl>} />}
    <DetailsPanel title="Comprendre les mesures"><p className="backup-association">CPU : 100 % correspond à un cœur utilisé. La mémoire exclut le cache inactif.
      Sans limite explicite, la limite effective peut correspondre à la RAM de l’hôte.
      Un conteneur démarré ne garantit pas le fonctionnement de Moodle.</p></DetailsPanel>
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
