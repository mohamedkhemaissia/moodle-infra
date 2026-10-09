import { useState, useEffect } from 'react';
import { useReadings } from './useReadings.js';
import { catalogCurrent, catalogPath } from './backupCatalog.js';

const dates = new Intl.DateTimeFormat('fr-FR', { dateStyle: 'short', timeStyle: 'medium' });
const date = (value) => value ? dates.format(Date.parse(value)) : 'Non enregistré';
const reasons = {
  catalog_file_missing: 'Le catalogue n’est pas encore publié.',
  catalog_file_invalid: 'Le catalogue reçu est inexploitable.',
  repository_unavailable: 'Le dépôt de sauvegarde n’est pas accessible.',
  repository_timeout: 'La lecture du dépôt a dépassé le délai prévu.',
  repository_output_too_large: 'Le dépôt dépasse la taille de catalogue prévue.',
  repository_output_invalid: 'Les références retournées par le dépôt sont inexploitables.',
  scope_metadata_invalid: 'Certains points ne correspondent pas au périmètre attendu.',
  collection_stale: 'Le catalogue est ancien. La présence actuelle des points reste à vérifier.',
  clock_inconsistent: 'L’heure du catalogue est incohérente.',
  not_configured: 'Le catalogue de ces sauvegardes n’est pas activé.',
};

export function CatalogArea({ reading, now, scope, offset, onScope, onPage, onRequest }) {
  const value = reading.data;
  const current = catalogCurrent(value, now);
  return <div className="backup-area backup-catalog">
    <div className="backup-area-heading"><h3>Catalogue des points de reprise</h3>
      <span className={'ops-badge ' + (current ? 'good' : 'neutral')}>{current ? 'Catalogue récent' : 'État inconnu'}</span></div>
    <div className="catalog-tabs" role="group" aria-label="Type de sauvegarde">
      {[['moodle', 'Moodle'], ['pilotage', 'PostgreSQL du pilotage']].map(([key, label]) =>
        <button key={key} type="button" aria-pressed={scope === key} onClick={() => onScope(key)}>{label}</button>)}
    </div>
    {reading.pending ? <p className="ops-empty" role="status">Lecture du catalogue…</p> : !current ?
      <p className="ops-empty" role="status">{reading.error || reasons[value?.reason] ||
        (value?.status === 'available' || reading.stale ? reasons.collection_stale : 'Catalogue indisponible.')}</p> : <>
      <p className="ops-caption">{value.total} point{value.total > 1 ? 's' : ''} recensé{value.total > 1 ? 's' : ''}.
        {' '}Dépôt consulté le {date(value.collection.observed_at)}.</p>
      {value.latest_success_presence === 'absent' && <p className="catalog-warning" role="status">
        Le dernier point confirmé localement est absent du dépôt consulté. Vérifier le dépôt avant une reprise.</p>}
      {value.total === 0 ? <p className="ops-empty">Aucun point de ce type n’a été trouvé dans le dépôt.</p> : <>
        <div className="ops-table-wrap"><table className="catalog-table">
          <caption className="catalog-caption">Points de reprise {scope === 'moodle' ? 'Moodle' : 'PostgreSQL du pilotage'}</caption>
          <thead><tr><th>Référence</th><th>Début d’archivage</th><th>Instant des données</th><th>Hôte d’origine</th><th>Confirmation</th>{onRequest && <th>Test isolé</th>}</tr></thead>
          <tbody>{value.items.map((point) => <tr key={point.id}>
            <td><details><summary><code>{point.id.slice(0, 12)}</code></summary>
              <span className="catalog-id" aria-label="Identifiant complet du point">{point.id}</span></details></td>
            <td>{date(point.archived_at)}</td><td>{date(point.recovery_at)}</td><td>{point.hostname}</td>
            <td>{point.success_recorded ? 'Dernière sauvegarde confirmée' : 'Présent dans le dépôt'}</td>
            {onRequest && <td><button className="refresh-button" onClick={() => onRequest({ scope, point })}>
              Demander un test</button></td>}
          </tr>)}</tbody>
        </table></div>
        <div className="catalog-pagination">
          <button type="button" disabled={offset === 0} onClick={() => onPage(Math.max(0, offset-10))}>Précédent</button>
          <span>Page {Math.floor(offset/10)+1} sur {Math.max(1, Math.ceil(value.listed_total/10))}</span>
          <button type="button" disabled={!value.has_more} onClick={() => onPage(offset+10)}>Suivant</button>
        </div>
      </>}
      {value.truncated && <p className="ops-caption">Les 200 points les plus récents sont affichables ici sur {value.total} recensés.</p>}
      <p className="ops-section-note">La date d’archivage indique le début du transfert vers le dépôt.
        L’instant des données provient du dernier enregistrement de sauvegarde confirmé.
        La présence d’un point ne prouve pas sa restauration.</p>
    </>}
  </div>;
}

export default function BackupCatalog({ now, refreshToken, onRequest }) {
  const [selection, setSelection] = useState({ scope: 'moodle', offset: 0 });
  const { scope, offset } = selection;
  const readings = useReadings([{ key: 'catalog', kind: 'backup_catalog', path: catalogPath(scope, offset) }], refreshToken, now);
  useEffect(() => {
    const value = readings.catalog.data;
    if (value?.status !== 'available') return;
    const last = Math.max(0, Math.ceil(value.listed_total / 10) - 1) * 10;
    if (offset > last) setSelection((previous) => ({ ...previous, offset: last }));
  }, [readings.catalog.data, offset]);
  return <CatalogArea reading={readings.catalog} now={now} scope={scope} offset={offset}
    onRequest={onRequest}
    onScope={(value) => setSelection({ scope: value, offset: 0 })}
    onPage={(value) => setSelection((previous) => ({ ...previous, offset: value }))} />;
}
