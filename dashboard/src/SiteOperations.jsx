import { useCallback, useEffect, useState } from 'react';
import { useAuth } from './Auth.jsx';
import { canManageUsers, jsonRequest } from './authClient.js';
import { operationKey } from './courseOperations.js';
import { siteOperationActive, validateSiteMutation, validateSiteOperations } from './siteOperations.js';

const dates = new Intl.DateTimeFormat('fr-FR', { dateStyle: 'short', timeStyle: 'short' });
const labels = { queued: 'Opération en attente…', running: 'Opération en cours…',
  completed: 'Opération réussie', failed: 'Échec de l’opération', interrupted: 'Opération interrompue' };

export default function SiteOperations({ refreshToken }) {
  const auth = useAuth();
  const [data, setData] = useState(null);
  const [selection, setSelection] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const load = useCallback(async (signal) => {
    try {
      const value = validateSiteOperations(await jsonRequest('/api/v1/site-operations', { signal }));
      setData(value); setError('');
      setSelection((before) => value.points.some((point) => point.id === before)
        ? before : value.points[0]?.id || '');
    } catch (reason) {
      if (reason?.name !== 'AbortError') {
        setData(null); setError(reason?.message || 'Sauvegarde du site indisponible.');
      }
    }
  }, []);
  useEffect(() => {
    const controller = new AbortController(); load(controller.signal);
    return () => controller.abort();
  }, [load, refreshToken]);
  const current = data?.operations[0];
  useEffect(() => {
    if (!siteOperationActive(current)) return undefined;
    const timer = setInterval(() => load(), 2000);
    return () => clearInterval(timer);
  }, [current, load]);
  async function submit(path, body) {
    setBusy(true); setError('');
    try {
      const result = validateSiteMutation(await jsonRequest(path, {
        method: 'POST', csrfToken: auth.csrfToken, body: { ...body, client_key: operationKey() },
      }));
      setData((before) => ({ ...before, operations: [result.operation,
        ...(before?.operations || []).filter((row) => row.id !== result.operation.id)] }));
      setConfirmation('');
    } catch (reason) { setError(reason?.message || 'Demande refusée.'); }
    finally { setBusy(false); }
  }
  const allowed = canManageUsers(auth.user);
  const active = busy || siteOperationActive(current);
  const protectedSite = data?.latest_success_at
    && Date.now() - Date.parse(data.latest_success_at) <= 26 * 60 * 60 * 1000;
  return <div className="site-backup-card">
    <div className="backup-area-heading"><div><h3>Sauvegarde complète du site</h3>
      <p className="ops-caption">Protection de Moodle et de ses données.</p></div>
      <span className={`ops-badge ${protectedSite ? 'good' : 'warning'}`}>
        {protectedSite ? 'Protégé' : 'Attention'}</span></div>
    {error && <p className="notice" role="alert">{error}</p>}
    {!data ? !error && <p role="status">Lecture de l’état…</p> : <>
      <div className="site-backup-latest"><span>Dernière sauvegarde</span>
        <strong>{data.latest_success_at ? dates.format(Date.parse(data.latest_success_at)) : 'Aucune sauvegarde'}</strong></div>
      <button className="primary-button" disabled={!allowed || active}
        onClick={() => submit('/api/v1/site-operations/backups', {})}>Sauvegarder maintenant</button>
      <div className="site-restore-form"><h4>Restaurer un point de reprise</h4>
        <label>Date<select value={selection} disabled={!allowed || active || data.points.length === 0}
          onChange={(event) => setSelection(event.target.value)}>
          {data.points.length === 0 && <option value="">Aucun point disponible</option>}
          {data.points.map((point) => <option key={point.id} value={point.id}>
            {dates.format(Date.parse(point.created_at))}</option>)}</select></label>
        <label>Pour confirmer, saisir RESTAURER<input value={confirmation} maxLength={10}
          disabled={!allowed || active || !selection} onChange={(event) => setConfirmation(event.target.value)} /></label>
        <button className="secondary-button" disabled={!allowed || active || !selection || confirmation !== 'RESTAURER'}
          onClick={() => submit('/api/v1/site-operations/restores', { snapshot_id: selection,
            confirmation: 'RESTAURER' })}>Restaurer</button></div>
      {current && <p className={`site-operation-state ${current.status}`} role="status">
        {labels[current.status]}</p>}
      {!allowed && <p className="ops-section-note">Un compte administrateur est requis pour lancer une opération.</p>}
    </>}
  </div>;
}
