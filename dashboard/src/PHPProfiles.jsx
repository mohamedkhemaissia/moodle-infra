import { useReadings } from './useReadings.js';
import { PROFILE_NAMES, PROFILE_STATES } from './phpProfiles.js';
import './backups.css';

const requests = [{ key: 'profiles', kind: 'php_profiles', path: '/api/v1/php-fpm/profiles' }];
const date = (value) => new Intl.DateTimeFormat('fr-FR', { dateStyle: 'short', timeStyle: 'medium' }).format(new Date(value));

export default function PHPProfiles({ now, refreshToken }) {
  const { profiles: reading } = useReadings(requests, refreshToken, now);
  const data = reading.data;
  return <section id="php-profiles" className="panel ops-panel" aria-labelledby="php-profiles-heading">
    <div className="panel-heading"><div><h2 id="php-profiles-heading">Profils PHP-FPM</h2>
      <p>Capacité du pool et suivi des changements autorisés.</p></div></div>
    <div className="ops-body">{reading.pending ? <p role="status">Lecture des profils…</p> :
      reading.error || reading.stale ? <p role="status">{reading.error || 'Actualisez pour relire les profils.'}</p> :
      data?.status === 'unavailable' ? <p>Les profils ne sont pas encore préparés ou leur suivi est indisponible.</p> : data && <>
        <div className="backup-area-heading"><h3>{PROFILE_STATES[data.state]}</h3>
          <span className={'ops-badge ' + (data.review_required ? 'warning' : 'neutral')}>Consultation</span></div>
        <dl className="backup-grid">{data.profiles.map((p) => <div key={p.id}><dt>Profil {PROFILE_NAMES[p.id]}</dt>
          <dd>{p.max_children} processus maximum</dd></div>)}</dl>
        <p>Dernier profil enregistré : {PROFILE_NAMES[data.recorded_profile] || 'À vérifier'} · {date(data.recorded_at)}.</p>
        <p className="ops-caption">Une file d’attente confirmée et des ressources suffisantes sont nécessaires avant un changement.
          Un retour au profil précédent est tenté si les contrôles après changement échouent.</p>
        {data.review_required && <p role="alert">Le changement précédent doit être examiné avant toute nouvelle intervention.</p>}
        {data.run_id && <a className="ops-json-link" href={'/api/v1/remediations/' + data.run_id} target="_blank" rel="noreferrer">Consulter le journal de l’intervention ↗</a>}
      </>}</div>
  </section>;
}
