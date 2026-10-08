import { useReadings } from './useReadings.js';
import { backupView } from './backups.js';
import './backups.css';

const requests = [{ key: 'backup', kind: 'backup', path: '/api/v1/backups/summary' }];
const numbers = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 1 });
const dates = new Intl.DateTimeFormat('fr-FR', { dateStyle: 'short', timeStyle: 'medium' });
const reasons = {
  status_file_missing: 'Le suivi des sauvegardes ne fournit pas encore de données.',
  status_file_invalid: 'Les informations de sauvegarde sont inexploitables.',
  collection_stale: 'Le suivi des sauvegardes est ancien. Son état actuel est inconnu.',
  clock_inconsistent: 'L’heure du suivi est incohérente.',
  success_marker_missing: 'Aucun point de sauvegarde réussi n’est enregistré.',
  success_marker_invalid: 'Le dernier point enregistré ne peut pas être interprété.',
};
const labels = { recent: 'Point récent', overdue: 'Point trop ancien', missing: 'Point absent', unknown: 'État inconnu' };
const tone = { recent: 'good', overdue: 'warning', missing: 'warning', unknown: 'neutral' };
const date = (value) => value ? dates.format(Date.parse(value)) : '—';

export default function Backups({ now, refreshToken }) {
  const { backup: reading } = useReadings(requests, refreshToken, now);
  const data = reading.data;
  const view = backupView(data, now);
  return <section id="backups" className="panel ops-panel" aria-labelledby="backups-heading">
    <div className="panel-heading"><div><h2 id="backups-heading">Sauvegardes</h2>
      <p>Point de reprise Moodle et suivi de la planification.</p></div>
      <span className={'ops-badge ' + tone[view.state]}>{labels[view.state]}</span></div>
    <div className="ops-body">
      {reading.pending ? <p className="ops-empty" role="status">Lecture en cours…</p> :
        !data || data.status !== 'available' || view.reason === 'collection_stale' ?
          <p className="ops-empty" role="status">{reading.error || reasons[view.reason] ||
            (reading.stale ? reasons.collection_stale : 'Suivi des sauvegardes indisponible.')}</p> :
          <>
            {view.reason && <p className="ops-empty" role="status">{reasons[view.reason] || 'Point de reprise inconnu.'}</p>}
            <dl className="backup-grid">
              <div><dt>Point de reprise</dt><dd>{date(data.latest_success?.frozen_at_utc)}</dd></div>
              <div><dt>Âge du point</dt><dd>{Number.isFinite(view.ageHours) ?
                numbers.format(view.ageHours) + ' h' : '—'}</dd></div>
              <div><dt>Seuil d’ancienneté</dt><dd>{numbers.format(data.policy.max_age_hours)} h</dd></div>
              <div><dt>Planification</dt><dd>{{ active: 'Active', inactive: 'Inactive', unknown: 'Inconnue' }[data.schedule.state]}</dd>
                <small>Démarrage automatique : {data.schedule.enabled === null ? 'inconnu' : data.schedule.enabled ? 'activé' : 'désactivé'}</small></div>
              <div><dt>Traitement</dt><dd>{{ running: 'En cours', idle: 'Au repos', unknown: 'Inconnu' }[data.job.state]}</dd></div>
              <div><dt>Dernier résultat du traitement</dt><dd>{{ success: 'Réussite', failed: 'Échec', unknown: 'Inconnu' }[data.job.last_result]}</dd></div>
            </dl>
            {data.latest_success && <p className="ops-caption">Sauvegarde terminée le {date(data.latest_success.completed_at_utc)}.
              {' '}Référence : <code>{data.latest_success.snapshot.slice(0, 12)}</code>.</p>}
          </>}
      <p className="ops-section-note">Cette vue suit la fraîcheur du point enregistré.
        Les contrôles d’intégrité et de restauration sont distincts.</p>
    </div>
  </section>;
}
