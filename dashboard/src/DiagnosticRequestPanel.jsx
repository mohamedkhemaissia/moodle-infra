import { useEffect, useRef, useState } from 'react';
import { useAuth } from './Auth.jsx';
import { canManageUsers, jsonRequest } from './authClient.js';
import { CompactTable, DetailsPanel, StatusBadge } from './ApplicationServices.jsx';
import { RunSummary } from './ReadableDetails.jsx';
import { validateDocument } from './operations.js';
import {
  diagnosticActive,
  diagnosticClientKey,
  diagnosticRequestLabels,
  diagnosticRequestsPath,
  latestDiagnosticRequest,
  validateDiagnosticMutation,
  validateDiagnosticRequests,
} from './diagnosticRequests.js';

const dates = new Intl.DateTimeFormat('fr-FR', { dateStyle: 'short', timeStyle: 'medium' });
const date = (value) => dates.format(Date.parse(value));

function requestMessage(request) {
  if (!request) return 'Aucune demande de diagnostic enregistrée pour cet incident.';
  if (request.status === 'queued') return 'La demande est enregistrée et attend sa prise en charge par le worker.';
  if (request.status === 'running') return 'Le diagnostic en lecture seule est en cours. Cette vue se met à jour automatiquement.';
  if (request.status === 'completed') return 'Le diagnostic en lecture seule est terminé. Son résultat est présenté ci-dessous.';
  return 'Le diagnostic n’a pas produit de résultat complet. Le motif technique est conservé dans la demande.';
}

export default function DiagnosticRequestPanel({ incidentId, eligible, refreshToken = 0, onNavigate }) {
  const auth = useAuth();
  const [reload, setReload] = useState(0);
  const [reading, setReading] = useState({ pending: true, value: null, result: null, error: null, resultError: null });
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState(null);
  const [mutationError, setMutationError] = useState(null);
  const retryKey = useRef(null);
  const admin = auth.enabled && canManageUsers(auth.user);
  const latest = latestDiagnosticRequest(reading.value);
  const active = diagnosticActive(latest);

  useEffect(() => {
    let live = true;
    let pollTimer;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    setReading((before) => before.incidentId === incidentId
      ? { ...before, pending: true, error: null, resultError: null }
      : { incidentId, pending: true, value: null, result: null, error: null, resultError: null });
    async function load() {
      try {
        const value = validateDiagnosticRequests(await jsonRequest(diagnosticRequestsPath(incidentId), {
          signal: controller.signal,
        }), incidentId);
        const current = latestDiagnosticRequest(value);
        let result = null;
        let resultError = null;
        if (current?.result_url) {
          try { result = validateDocument('run', await jsonRequest(current.result_url, { signal: controller.signal })); }
          catch (cause) { resultError = cause.name === 'AbortError' ? 'Le délai de lecture du résultat est dépassé.' : cause.message; }
        }
        if (live) {
          setReading({ incidentId, pending: false, value, result, error: null, resultError });
          if (diagnosticActive(current)) pollTimer = setTimeout(() => setReload((valueBefore) => valueBefore + 1), 4000);
        }
      } catch (cause) {
        if (live) setReading({ incidentId, pending: false, value: null, result: null,
          error: cause.name === 'AbortError' ? 'Le délai de lecture des diagnostics est dépassé.' : cause.message,
          resultError: null });
      } finally { clearTimeout(timeout); }
    }
    load();
    return () => { live = false; clearTimeout(timeout); clearTimeout(pollTimer); controller.abort(); };
  }, [incidentId, refreshToken, reload]);

  async function requestDiagnostic() {
    if (!admin || !eligible || active || busy) return;
    setBusy(true); setNotice(null); setMutationError(null);
    const clientKey = retryKey.current || diagnosticClientKey();
    retryKey.current = clientKey;
    try {
      const response = validateDiagnosticMutation(await jsonRequest('/api/v1/diagnostic-requests', {
        method: 'POST', csrfToken: auth.csrfToken, body: { incident_id: incidentId, client_key: clientKey },
        signal: AbortSignal.timeout(12000),
      }), incidentId);
      retryKey.current = null;
      setNotice(response.created ? 'Demande enregistrée. Le diagnostic sera exécuté en lecture seule.' :
        'Cette demande était déjà enregistrée ; son suivi a été actualisé.');
      setReload((value) => value + 1);
    } catch (cause) {
      setMutationError(cause.name === 'TimeoutError' ?
        'La réponse tarde. Réessayez : la même clé sera utilisée pour éviter une seconde demande.' : cause.message);
    } finally { setBusy(false); }
  }

  return <section className="diagnostic-request-panel" aria-labelledby={`diagnostic-request-${incidentId}`}>
    <div className="diagnostic-request-heading"><div><h4 id={`diagnostic-request-${incidentId}`}>Diagnostic guidé</h4>
      <p>Demande administrateur → worker → Ansible Runner, exclusivement en lecture seule.</p></div>
      {latest && <StatusBadge state={latest.status} label={diagnosticRequestLabels[latest.status]} />}</div>
    {reading.pending && !reading.value && <p className="ops-empty" role="status">Lecture des demandes de diagnostic…</p>}
    {reading.error && <p className="catalog-warning" role="alert">{reading.error}</p>}
    {!reading.error && reading.value && <>
      <p className="ops-caption" role="status">{requestMessage(latest)}</p>
      {latest && <dl className="summary-facts diagnostic-request-facts">
        <div><dt>Demande</dt><dd><code title={latest.id}>{latest.id.slice(0, 12)}</code></dd></div>
        <div><dt>Créée le</dt><dd>{date(latest.created_at)}</dd></div>
        <div><dt>Demandée par</dt><dd>{latest.requested_by.username}</dd></div>
        {latest.started_at && <div><dt>Démarrée le</dt><dd>{date(latest.started_at)}</dd></div>}
        {latest.finished_at && <div><dt>Terminée le</dt><dd>{date(latest.finished_at)}</dd></div>}
        {latest.reason_code && <div><dt>Motif technique</dt><dd><code>{latest.reason_code}</code></dd></div>}
      </dl>}
      {latest?.result_url && reading.pending && <p className="ops-empty" role="status">Lecture du résultat…</p>}
      {reading.resultError && <p className="catalog-warning" role="alert">
        Le résultat est enregistré, mais sa lecture est indisponible : {reading.resultError}</p>}
      {reading.result && <div className="diagnostic-result" aria-label="Résultat du diagnostic">
        <div className="overview-heading"><h4>Résultat du diagnostic</h4><StatusBadge state={reading.result.status} /></div>
        <RunSummary run={reading.result} />
      </div>}
      {reading.value.items.length > 0 && <DetailsPanel title={`Historique des demandes (${reading.value.total})`}>
        <CompactTable caption={`Demandes de diagnostic de l’incident ${incidentId}`} rows={reading.value.items}
          rowKey={(request) => request.id} columns={[
            { key: 'date', label: 'Création', render: (request) => date(request.created_at) },
            { key: 'status', label: 'État', render: (request) => <StatusBadge state={request.status}
              label={diagnosticRequestLabels[request.status]} /> },
            { key: 'request', label: 'Demande', render: (request) => <code title={request.id}>{request.id.slice(0, 12)}</code> },
          ]} />
        {reading.value.has_more && <p className="ops-note">Seules les 10 demandes les plus récentes sont affichées ici.</p>}
      </DetailsPanel>}
    </>}
    {notice && <p className="diagnostic-notice" role="status">{notice}</p>}
    {mutationError && <p className="catalog-warning" role="alert">{mutationError}</p>}
    <div className="diagnostic-request-actions">
      <button className="refresh-button" type="button" onClick={requestDiagnostic}
        disabled={!admin || !eligible || active || busy || reading.pending}>
        {busy ? 'Enregistrement…' : active ? 'Diagnostic en cours' : 'Demander un diagnostic'}
      </button>
      <button className="ops-button" type="button" disabled={reading.pending}
        onClick={() => setReload((value) => value + 1)}>Actualiser le suivi</button>
      {latest?.run_id && onNavigate && <button className="ops-button" type="button"
        onClick={() => onNavigate('audits', incidentId)}>Voir dans l’historique</button>}
    </div>
    {!admin && <p className="ops-note">La demande de diagnostic nécessite une session administrateur.</p>}
    {admin && !eligible && !active && <p className="ops-note">
      Une recommandation actuelle, fondée sur des observations récentes, est requise avant de lancer le diagnostic.</p>}
  </section>;
}
