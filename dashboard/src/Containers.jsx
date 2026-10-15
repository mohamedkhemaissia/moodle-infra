import { useState } from 'react';
import { useReadings } from './useReadings.js';
import { containerView } from './containerMetrics.js';
import { StatusBadge } from './ApplicationServices.jsx';
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
  const [selected, setSelected] = useState(null);
  return <>
    {reading.pending && <p role="status">Lecture des conteneurs…</p>}
    {reading.error && <p className="catalog-warning" role="status">{reading.error}</p>}
    {!reading.pending && !reading.error && !view.fresh && <p className="catalog-warning" role="status">
      Collecte indisponible ou ancienne. Les états et les ressources sont inconnus.</p>}
    {!reading.pending && !reading.error && view.fresh && view.rows.every((row) => row.state === 'unknown') &&
      <p className="catalog-warning" role="status">Les mesures des conteneurs Moodle ne sont pas encore disponibles.</p>}
    {view.rows.length > 0 && <div className="container-card-grid" aria-label="Conteneurs essentiels de Moodle">
      {view.rows.map((row) => { const key = `${row.project}/${row.service}`; const open = selected === key;
        const stateTone = row.state === 'running' ? 'operational' :
          ['unknown', 'ambiguous'].includes(row.state) ? reading.stale ? 'stale' : 'unknown' : 'warning';
        const healthTone = row.health === 'healthy' ? 'operational' : row.health === 'unhealthy' ? 'degraded' :
          row.health === 'starting' ? 'warning' : 'unknown';
        return <article className={`container-card${open ? ' selected' : ''}`} key={key}>
          <div className="container-card-heading"><div><h3>{serviceNames[row.service] || row.service}</h3><p>{row.project}</p></div>
            <StatusBadge state={stateTone} label={names[row.state]} /></div>
          <div className="container-health"><span>Santé</span><StatusBadge state={healthTone}
            label={healthNames[row.health] || 'À confirmer'} /></div>
          <dl className="container-card-metrics"><div><dt>CPU</dt><dd>{number(row.metrics.cpu_usage_percent, ' %')}</dd></div>
            <div><dt>Mémoire</dt><dd>{number(row.metrics.memory_working_set_bytes === null ? null :
              row.metrics.memory_working_set_bytes / 1048576, ' Mio')}</dd></div></dl>
          <button className="ops-button container-detail-button" type="button" aria-expanded={open}
            onClick={() => setSelected(open ? null : key)}>{open ? 'Masquer les détails' : 'Consulter'}</button>
          {open && <dl className="container-card-details"><div><dt>Limite mémoire</dt><dd>{number(
            row.metrics.memory_limit_bytes === null ? null : row.metrics.memory_limit_bytes / 1048576, ' Mio')}</dd></div>
            <div><dt>Processus</dt><dd>{number(row.metrics.pids)}</dd></div>
            <div><dt>Instances</dt><dd>{view.fresh ? row.instances ?? '—' : '—'}</dd></div>
            <div><dt>Contrôle Docker</dt><dd>{healthNames[row.health] || 'Aucun contrôle confirmé'}</dd></div>
            <div><dt>Âge des statistiques</dt><dd>{number(row.stats_age_seconds, ' s')}</dd></div></dl>}
        </article>;
      })}
    </div>}
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
