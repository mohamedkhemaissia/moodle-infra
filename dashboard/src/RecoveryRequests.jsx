import { useState } from 'react';
import { useAuth } from './Auth.jsx';
import { useReadings } from './useReadings.js';
import { jsonRequest } from './authClient.js';
import { canManageRecovery, approvalCurrent, canStartTest, canCancelRequest, executionLabels, requestLabels, requestKey, requestPath, validateRecoveryMutation } from './recoveryRequests.js';

const dates = new Intl.DateTimeFormat('fr-FR', { dateStyle: 'short', timeStyle: 'medium' });
const date = (v) => dates.format(Date.parse(v));
const scopeName = (v) => v === 'moodle' ? 'Moodle' : 'PostgreSQL du pilotage';
const eventLabels = { created: 'Demande créée', approved: 'Demande approuvée', rejected: 'Demande refusée', cancelled: 'Demande annulée' };

async function write(path, options) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  try { return validateRecoveryMutation(await jsonRequest(path, { ...options, method: 'POST', signal: controller.signal })); }
  finally { clearTimeout(timeout); }
}

export function RequestForm({ selection, auth, onCreated, onClose }) {
  const [reason, setReason] = useState('');
  const [key, setKey] = useState(() => requestKey());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  async function submit(event) {
    event.preventDefault(); setBusy(true); setError(null);
    try {
      const result = await write('/api/v1/recovery/requests', { csrfToken: auth.csrfToken,
        body: { scope: selection.scope, snapshot_id: selection.point.id, reason, client_key: key } });
      onCreated(result.request.id);
    } catch (cause) { setError(cause.name === 'AbortError' ? 'Délai dépassé. Réessayez la même demande.' : cause.message); }
    finally { setBusy(false); }
  }
  return <div className="recovery-form-card"><h4>Demander un test de restauration</h4>
    <p>{scopeName(selection.scope)} · <strong>environnement isolé</strong></p>
    <code className="recovery-reference">{selection.point.id}</code>
    <form className="auth-form recovery-form" onSubmit={submit}>
      <label>Motif de la demande<input required minLength={15} maxLength={500} value={reason} disabled={busy}
        onChange={(e) => { setReason(e.target.value); setKey(requestKey()); }} placeholder="Précisez le point et le contrôle souhaité" /></label>
      <p className="ops-caption">La demande sera enregistrée pour approbation. Cette étape ne lance aucun test.</p>
      {error && <p className="auth-error" role="alert">{error}</p>}
      <div className="recovery-actions"><button className="auth-primary" disabled={busy} type="submit">
        {busy ? 'Enregistrement…' : 'Enregistrer la demande'}</button>
      <button className="refresh-button" type="button" disabled={busy} onClick={onClose}>Fermer</button></div>
    </form>
  </div>;
}

export function RequestDetail({ value, now, admin, busy, onDecision, onClose }) {
  const [reason, setReason] = useState('');
  const [confirmation, setConfirmation] = useState(false);
  const row = value.request;
  return <div className="recovery-form-card">
    <div className="backup-area-heading"><h4>Demande · {requestLabels[row.status]}</h4>
      <button className="refresh-button" type="button" disabled={busy} onClick={onClose}>Fermer le détail</button></div>
    <p>{scopeName(row.scope)} · Test isolé</p><code className="recovery-reference">{row.snapshot_id}</code>
    <p>{row.reason}</p><p className="ops-caption">Demandée par {row.requested_by.username} le {date(row.created_at)}.</p>
    {row.decision && <p className="ops-caption">Décision de {row.decision.by.username} le {date(row.decision.at)} : {row.decision.reason}</p>}
    {row.status === 'approved' && ['not_dispatched','queued'].includes(row.execution.status) && <p className="ops-caption">{approvalCurrent(row, now)
      ? `Approbation valable jusqu’au ${date(row.decision.valid_until)}.`
      : 'Approbation expirée : une nouvelle demande est nécessaire.'}</p>}
    <p className="ops-section-note">{executionLabels[row.execution.status]}. {row.execution.status === 'not_dispatched'
      ? 'Le test exige un lancement explicite après approbation.' : 'Le test utilise une copie séparée de la sauvegarde.'}</p>
    {row.execution.started_at && <p className="ops-caption">Démarrage : {date(row.execution.started_at)}{row.execution.finished_at && ` · Fin : ${date(row.execution.finished_at)}`}</p>}
    {row.execution.reason_code && <p className="catalog-warning">{resultReasons[row.execution.reason_code] || 'Le test n’a pas apporté toutes les preuves nécessaires. Consultez son historique.'}</p>}
    {row.execution.report && <ExecutionReport report={row.execution.report} />}
    {admin && canStartTest(row,now) && <div className="auth-form recovery-form">
      <label className="recovery-confirm"><input type="checkbox" checked={confirmation} disabled={busy}
        onChange={(e)=>setConfirmation(e.target.checked)} />Je confirme le point affiché et le lancement du test isolé.</label>
      <button className="auth-primary" disabled={busy || !confirmation} onClick={()=>onDecision(row,'execute')}>Lancer le test isolé</button>
    </div>}
    {admin && canCancelRequest(row) && <div className="auth-form recovery-form">
      <label>Motif de la décision<input minLength={15} maxLength={500} value={reason} disabled={busy}
        onChange={(e) => setReason(e.target.value)} /></label>
      {row.status === 'pending' && <label className="recovery-confirm">
        <input type="checkbox" checked={confirmation} disabled={busy} onChange={(e) => setConfirmation(e.target.checked)} />
        Je confirme la référence complète affichée et le test en environnement isolé.</label>}
      <div className="recovery-actions">
        {row.status === 'pending' && <>
          <button className="auth-primary" disabled={busy || reason.trim().length < 15 || !confirmation}
            onClick={() => onDecision(row, 'approve', reason)}>Approuver</button>
          <button className="refresh-button" disabled={busy || reason.trim().length < 15}
            onClick={() => onDecision(row, 'reject', reason)}>Refuser</button>
        </>}
        <button className="refresh-button" disabled={busy || reason.trim().length < 15}
          onClick={() => onDecision(row, 'cancel', reason)}>Annuler la demande</button>
      </div>
    </div>}
    <h4 className="recovery-history-title">Historique des décisions</h4>
    <ol className="recovery-history">{value.events.map((event) => <li key={event.id}>
      <strong>{eventLabels[event.action]}</strong> · {event.by.username} · {date(event.at)}
      {event.evidence.reason && <span>{event.evidence.reason}</span>}
    </li>)}</ol>
    {value.execution_events?.length > 0 && <><h4 className="recovery-history-title">Historique du test</h4>
      <ol className="recovery-history">{value.execution_events.map((event)=><li key={event.id}>
        <strong>{executionEventLabels[event.action]}</strong> · {event.by.kind === 'worker' ? 'Service de restauration' : event.by.username} · {date(event.at)}
      </li>)}</ol></>}
  </div>;
}

function Detail({ identifier, now, refreshToken, admin, busy, onDecision, onClose }) {
  const readings = useReadings([{ key: 'detail', kind: 'recovery_request', path: requestPath(identifier) }], refreshToken, now);
  const reading = readings.detail;
  return reading.data ? <RequestDetail key={reading.data.request.revision+':'+reading.data.request.execution.status} value={reading.data} now={now} admin={admin}
    busy={busy} onDecision={onDecision} onClose={onClose} /> : <p className="ops-empty" role="status">
    {reading.pending ? 'Lecture du détail…' : reading.error || 'Détail indisponible. Actualiser avant une décision.'}</p>;
}

export function RequestsArea({ reading, onOpen, offset, onPage, now }) {
  const value = reading.data;
  return <>
    {!value ? <p className="ops-empty" role="status">{reading.pending ? 'Lecture des demandes…' : reading.error || 'Journal des demandes indisponible.'}</p>
      : value.total === 0 ? <p className="ops-empty">Aucune demande enregistrée.</p> : <>
        <div className="ops-table-wrap"><table className="catalog-table"><thead><tr>
          <th>Création</th><th>Sauvegarde</th><th>Demandeur</th><th>Décision</th><th>Test</th><th>Détail</th>
        </tr></thead><tbody>{value.items.map((row) => <tr key={row.id}>
          <td>{date(row.created_at)}</td><td>{scopeName(row.scope)}<br /><code>{row.snapshot_id.slice(0, 12)}</code></td>
          <td>{row.requested_by.username}</td><td>{row.status === 'approved' && row.execution.status === 'not_dispatched' && !approvalCurrent(row, now) ? 'Approbation expirée' : requestLabels[row.status]}</td>
          <td>{executionLabels[row.execution.status]}</td>
          <td><button className="refresh-button" onClick={() => onOpen(row.id)}>Consulter</button></td>
        </tr>)}</tbody></table></div>
        <div className="catalog-pagination"><button disabled={offset === 0} onClick={() => onPage(Math.max(0, offset-10))}>Précédent</button>
          <span>{value.total} demande{value.total > 1 ? 's' : ''}</span>
          <button disabled={!value.has_more || offset >= 10000} onClick={() => onPage(offset+10)}>Suivant</button></div>
      </>}
  </>;
}

export default function RecoveryRequests({ now, refreshToken, selection, onCloseSelection }) {
  const auth = useAuth();
  const admin = canManageRecovery(auth);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(null);
  const [changed, setChanged] = useState(0);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);
  const token = `${refreshToken}:${changed}`;
  const readings = useReadings(auth?.enabled ? [{ key: 'requests', kind: 'recovery_requests',
    path: `/api/v1/recovery/requests?limit=10&offset=${offset}` }] : [], token, now);
  async function decision(row, action, reason) {
    setBusy(true); setNotice(null);
    try {
      await write(requestPath(row.id) + '/' + action, { csrfToken: auth.csrfToken,
        body: action === 'execute' ? {revision:row.revision,confirmed_snapshot_id:row.snapshot_id} : { revision: row.revision, reason, ...(action === 'approve' ? { confirmed_snapshot_id: row.snapshot_id } : {}) } });
      setNotice(action === 'execute' ? 'Test placé en attente. Son résultat apparaîtra ici.' : 'Décision enregistrée.');
    } catch (error) { setNotice(error.name === 'AbortError' ? 'Délai dépassé. Consultez l’historique avant de renouveler la décision.' : error.message); }
    finally { setBusy(false); setChanged((value) => value+1); }
  }
  if (!auth?.enabled) return null;
  return <div className="backup-area" id="recovery-requests">
    <div className="backup-area-heading"><h3>Demandes de test de restauration</h3><span className="ops-badge neutral">Approbation</span></div>
    <p className="ops-caption">Choisissez un point dans le catalogue. Les administrateurs enregistrent les demandes et les décisions ; les lecteurs consultent leur historique.</p>
    {notice && <p className="catalog-warning" role="status">{notice}</p>}
    {admin && selection && <RequestForm key={selection.scope+selection.point.id} selection={selection} auth={auth}
      onClose={onCloseSelection} onCreated={(id) => { setSelected(id); setOffset(0); setChanged((value) => value+1); onCloseSelection(); setNotice('Demande enregistrée. Consultez son état et ses décisions.'); }} />}
    <RequestsArea reading={readings.requests} offset={offset} now={now} onPage={setOffset} onOpen={(id) => { setSelected(id); onCloseSelection(); }} />
    {selected && <Detail key={selected} identifier={selected} now={now} refreshToken={token} admin={admin} busy={busy}
      onDecision={decision} onClose={() => setSelected(null)} />}
  </div>;
}

const executionEventLabels = {queued:'Lancement demandé',started:'Test démarré',blocked:'Démarrage bloqué',finished:'Résultat enregistré',cancelled:'Test annulé',interrupted:'Exécution interrompue'};
const resultReasons = {authorization_or_catalog_unusable:'L’autorisation ou le catalogue ne permet plus le démarrage. Créez une nouvelle demande.',
  isolated_test_cleanup_incomplete:'Le nettoyage du test doit être terminé avant une nouvelle exécution.',
  execution_interrupted_or_deadline:'Le test a été interrompu ou a dépassé sa durée autorisée.',
  cancelled_before_start:'Test annulé avant son démarrage.',insufficient_test_space:'Espace insuffisant pour créer la copie de test.'};
export function ExecutionReport({report}) {
  const labels={files_verified:'Fichiers vérifiés',tables_verified:'Tables vérifiées',rows_verified:'Lignes vérifiées',courses:'Cours restaurés',users:'Comptes restaurés',quizzes:'Quiz restaurés'};
  return <div className="recovery-result"><h4>Vérifications du test</h4>
    <p>{report.status === 'restore_validated' ? 'Les contrôles de restauration ont réussi.' : 'La restauration n’est pas validée.'}</p>
    <dl>{Object.entries(labels).filter(([key])=>key in report).map(([key,label])=><div key={key}><dt>{label}</dt><dd>{report[key]}</dd></div>)}</dl>
    {report.login_check === 'passed' && <p>Page de connexion Moodle vérifiée en HTTPS.</p>}
    {report.database_check === 'passed' && <p>Base restaurée vérifiée.</p>}
    <p>{Object.values(report.cleanup).every(Boolean) ? 'Environnement de test supprimé.' : 'Nettoyage incomplet.'}</p>
    <p className="ops-caption">La plateforme en service n’a pas été modifiée par ce test.</p>
  </div>;
}
