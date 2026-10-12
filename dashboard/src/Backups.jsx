import { useReadings } from './useReadings.js';
import { backupView, pilotageRestoreView } from './backups.js';
import './backups.css';
import BackupCatalog from './BackupCatalog.jsx';
import { useState } from 'react';
import RecoveryRequests from './RecoveryRequests.jsx';
import { DetailsPanel, StatusBadge } from './ApplicationServices.jsx';

const requests = [
  { key: 'backup', kind: 'backup', path: '/api/v1/backups/summary' },
  { key: 'pilotage', kind: 'pilotage_backup', path: '/api/v1/backups/pilotage/summary' },
];
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
const restorationLabels = { validated: 'Point actuel validé', not_tested: 'Point actuel non testé',
  failed: 'Dernier test en échec', inconclusive: 'Test non concluant', unknown: 'Validation inconnue' };
const date = (value) => value ? dates.format(Date.parse(value)) : '—';

export function BackupArea({ reading, now, pilotage = false }) {
  const data = reading.data;
  const view = backupView(data, now);
  const restore = pilotageRestoreView(pilotage ? data : null, now);
  const readable = data?.status === 'available' && view.reason !== 'collection_stale';
  return <div className="backup-area">
    <div className="backup-area-heading"><h3>{pilotage ? 'PostgreSQL du pilotage' : 'Moodle'}</h3>
      <StatusBadge state={view.state === 'recent' ? 'operational' : view.state === 'unknown' ? 'unknown' : 'warning'} label={labels[view.state]} /></div>
    {reading.pending ? <p className="ops-empty" role="status">Lecture en cours…</p> : !readable ?
      <p className="ops-empty" role="status">{reading.error || reasons[view.reason] ||
        (reading.stale ? reasons.collection_stale : 'Suivi des sauvegardes indisponible.')}</p> : <>
      {view.reason && <p className="ops-empty" role="status">{reasons[view.reason] || 'Point de reprise inconnu.'}</p>}
      <dl className="backup-grid">
        <div><dt>Point de reprise</dt><dd>{date(data.latest_success?.frozen_at_utc)}</dd></div>
        <div><dt>Âge du point</dt><dd>{Number.isFinite(view.ageHours) ? numbers.format(view.ageHours) + ' h' : '—'}</dd></div>
        <div><dt>Planification</dt><dd>{{ active: 'Active', inactive: 'Inactive', unknown: 'Inconnue' }[data.schedule.state]}</dd></div>
        {pilotage && <>
          <div><dt>Données sauvegardées</dt><dd>{data.latest_success ? numbers.format(data.latest_success.tables) + ' tables' : '—'}</dd>
            <small>{data.latest_success ? numbers.format(data.latest_success.rows) + ' enregistrements' : 'Point absent ou inconnu'}</small></div>
          <div><dt>Contrôle d’intégrité</dt><dd>{data.latest_success ? 'Réussi' : 'Inconnu'}</dd>
            <small>Résultat enregistré lors de cette sauvegarde</small></div>
          <div><dt>Restauration du point actuel</dt><dd>{restorationLabels[restore.state]}</dd></div>
        </>}
      </dl>
      <DetailsPanel title="Détails du suivi de sauvegarde">
        <dl className="summary-facts"><div><dt>Seuil d’ancienneté</dt><dd>{numbers.format(data.policy.max_age_hours)} h</dd></div>
          <div><dt>Démarrage automatique</dt><dd>{data.schedule.enabled === null ? 'Inconnu' : data.schedule.enabled ? 'Activé' : 'Désactivé'}</dd></div>
          <div><dt>Traitement</dt><dd>{{ running: 'En cours', idle: 'Au repos', unknown: 'Inconnu' }[data.job.state]}</dd></div>
          <div><dt>Dernier résultat</dt><dd>{{ success: 'Réussite', failed: 'Échec', unknown: 'Inconnu' }[data.job.last_result]}</dd></div>
        </dl>
        {data.latest_success && <><p className="ops-caption">Sauvegarde terminée le {date(data.latest_success.completed_at_utc)}.</p>
          <code className="recovery-reference">{data.latest_success.snapshot}</code>
          <p className="ops-caption">Durée : {numbers.format(data.latest_success.duration_seconds)} s.</p></>}
      </DetailsPanel>
      {pilotage && <DetailsPanel title="Preuves de restauration PostgreSQL" className="backup-restore">
        <div className="backup-area-heading"><h4>Dernier test de restauration isolée</h4>
          <span className={'ops-badge ' + (restore.test?.status === 'restore_validated' ? 'good' :
            restore.test ? 'warning' : 'neutral')}>
            {restore.test ? ({ restore_validated: 'Test réussi', failed: 'Échec', inconclusive: 'Non concluant' }[restore.test.status]) : 'Aucun résultat exploitable'}
          </span></div>
        {restore.test ? <>
          <p className="ops-caption">Terminé le {date(restore.test.completed_at)}.
            {' '}Point testé : <code>{restore.test.snapshot.slice(0, 12)}</code>.</p>
          <p className="backup-association">{restore.sameSnapshot === true ? 'Ce test concerne le point de sauvegarde actuel.' :
            restore.sameSnapshot === false ? 'Ce test concerne un autre point. La sauvegarde actuelle reste à tester.' :
              'Aucun point actuel exploitable ne permet de rapprocher ce test.'}</p>
          <dl className="backup-grid">
            <div><dt>Données vérifiées</dt><dd>{restore.test.tables_verified === null ? '—' : numbers.format(restore.test.tables_verified) + ' tables'}</dd>
              <small>{restore.test.rows_verified === null ? 'Comptage non confirmé' : numbers.format(restore.test.rows_verified) + ' enregistrements'}</small></div>
            <div><dt>Durée du test</dt><dd>{restore.test.duration_seconds === null ? '—' : numbers.format(restore.test.duration_seconds) + ' s'}</dd></div>
            <div><dt>Nettoyage après le test</dt><dd>{restore.test.cleanup.container_removed && restore.test.cleanup.temporary_files_removed ? 'Terminé' : 'À vérifier'}</dd></div>
          </dl>
        </> : <p className="ops-empty">{data.restore_collection_status === 'invalid' ?
          'Les rapports de restauration sont inexploitables. La validation reste inconnue.' : 'Aucun test de restauration terminé n’est enregistré.'}</p>}
        <p className="ops-section-note">Ce test vérifie les données PostgreSQL dans un environnement isolé.</p>
      </DetailsPanel>}
    </>}
  </div>;
}

export default function Backups({ now, refreshToken }) {
  const [selection, setSelection] = useState(null);
  const readings = useReadings(requests, refreshToken, now);
  return <section id="backups" className="panel ops-panel" aria-labelledby="backups-heading">
    <div className="panel-heading"><div><h2 id="backups-heading">Sauvegardes</h2>
      <p>Protection de Moodle et consultation des points de reprise.</p></div></div>
    <div className="ops-body">
      <BackupArea reading={readings.backup} now={now} />
      <BackupCatalog now={now} refreshToken={refreshToken} />
      <DetailsPanel title="Gestion des restaurations" className="backup-secondary">
        <RecoveryRequests now={now} refreshToken={refreshToken} selection={selection}
          onSelectPoint={setSelection} onCloseSelection={() => setSelection(null)} />
      </DetailsPanel>
    </div>
  </section>;
}
