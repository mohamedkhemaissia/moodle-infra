import { useEffect, useState } from 'react';
import { useAuth } from './Auth.jsx';
import { canManageUsers, jsonRequest } from './authClient.js';
import { ACTION_NAMES, MODE_NAMES, approvalPayload, loadPolicies, policyPayload, validatePolicy } from './policies.js';
import './policies.css';
import { CompactTable, DetailsPanel } from './ApplicationServices.jsx';
import { SummaryList } from './ReadableDetails.jsx';

const date = (value) => new Intl.DateTimeFormat('fr-FR', { dateStyle: 'short', timeStyle: 'medium' }).format(new Date(value));
const key = () => crypto.randomUUID().replaceAll('-', '');
const workerNames = { unknown: 'Non observé', stale: 'Dernier passage ancien', idle: 'En attente', running: 'Traitement en cours', error: 'Indisponible' };
const draft = (policy) => ({ mode: policy.mode, suspended: policy.suspended, evaluation_context: policy.evaluation_context, reason: '', confirm_automatic: false });

export function PolicyView({ data, admin, form, change, save, approve, busy, approval, changeApproval }) {
  const policy = data.policy;
  return <>
    <dl className="policy-summary"><div><dt>Mode actif</dt><dd>{MODE_NAMES[policy.mode]}</dd></div>
      <div><dt>Corrections</dt><dd>{policy.suspended ? 'Suspendues' : policy.mode === 'observation' ? 'Désactivées' : 'Autorisées sous contrôle'}</dd></div>
      <div><dt>Période d’évaluation</dt><dd>{policy.evaluation_context ? 'Active — approbation requise' : 'Inactive'}</dd></div>
      <div><dt>Traitement des interventions</dt><dd>{workerNames[data.worker.status]}</dd></div></dl>
    <DetailsPanel title="Comprendre les modes et la version active">
      <p>Version {policy.revision} · {policy.updated_by} · {date(policy.updated_at)}</p>
      <p className="policy-description">Observation : consultation et diagnostics. Assisté : correction lancée manuellement depuis le serveur.
        Automatique : remise en service de PHP arrêté ou application d’un profil PHP-FPM préparé, selon les mesures et les ressources disponibles.</p>
    </DetailsPanel>
    {admin && <form className="auth-form policy-form" onSubmit={save}>
      <label>Mode de pilotage<select value={form.mode} disabled={busy} onChange={(e) => change('mode', e.target.value)}>
        {Object.entries(MODE_NAMES).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <label className="policy-check"><input type="checkbox" checked={form.suspended} disabled={busy} onChange={(e) => change('suspended', e.target.checked)} />Suspendre les nouvelles corrections</label>
      <label className="policy-check"><input type="checkbox" checked={form.evaluation_context} disabled={busy} onChange={(e) => change('evaluation_context', e.target.checked)} />Une évaluation est en cours</label>
      <label>Motif du changement<input required minLength={15} maxLength={500} value={form.reason} disabled={busy} onChange={(e) => change('reason', e.target.value)} /></label>
      {form.mode === 'automatic' && <label className="policy-check"><input type="checkbox" required checked={form.confirm_automatic} disabled={busy} onChange={(e) => change('confirm_automatic', e.target.checked)} />J’autorise la remise en service de PHP arrêté et les changements de profil PHP-FPM préparés, après vérification.</label>}
      <button className="refresh-button" disabled={busy}>{busy ? 'Enregistrement…' : 'Enregistrer la politique'}</button>
    </form>}
    {admin && policy.evaluation_context && !policy.suspended && policy.mode !== 'observation' && <details className="account-details">
      <summary>Approuver une intervention pendant l’évaluation</summary>
      <p>L’approbation concerne la correction proposée pour un incident Moodle ou une file d’attente PHP-FPM et expire avec ses observations, en 90 secondes au maximum.
        En mode assisté, l’administrateur lance ensuite la correction depuis le serveur.</p>
      <form className="auth-form policy-form" onSubmit={approve}>
        <label>Identifiant de l’incident<input type="number" required min={1} step={1} value={approval.incident} disabled={busy} onChange={(e) => changeApproval('incident', e.target.value)} /></label>
        <label>Motif d’approbation<input required minLength={15} maxLength={500} value={approval.reason} disabled={busy} onChange={(e) => changeApproval('reason', e.target.value)} /></label>
        <button className="refresh-button" disabled={busy}>Approuver l’intervention proposée</button>
      </form></details>}
    <DetailsPanel title="Historique des politiques" className="policy-history-heading">
    <p>{data.events.length} événements affichés sur {data.eventTotal}.</p>
    {data.events.length > 0 && <CompactTable caption="Audit des politiques" rows={data.events} rowKey={(row) => row.id} columns={[
      { key: 'date', label: 'Date', render: (row) => date(row.at) },
      { key: 'operation', label: 'Opération', render: (row) => ACTION_NAMES[row.action] },
      { key: 'author', label: 'Auteur', render: (row) => row.actor },
      { key: 'version', label: 'Version', render: (row) => row.revision },
    ]} details={(row) => <>
        <p>{row.details.after?.reason || row.details.reason || (row.details.incident_id ? `Incident ${row.details.incident_id}` : '—')}</p>
        <SummaryList rows={[
          ['Mode', MODE_NAMES[row.details.after?.mode] || null],
          ['Corrections', typeof row.details.after?.suspended === 'boolean' ? row.details.after.suspended ? 'Suspendues' : 'Sous contrôle de la politique' : null],
          ['Évaluation en cours', typeof row.details.after?.evaluation_context === 'boolean' ? row.details.after.evaluation_context ? 'Oui' : 'Non' : null],
        ]} />
      </>} />}
    {data.events.length === 0 && <p>Aucun changement enregistré.</p>}
    </DetailsPanel>
  </>;
}

export default function Policies({ refreshToken = 0 }) {
  const auth = useAuth();
  const [data, setData] = useState(null), [form, setForm] = useState(null);
  const [approval, setApproval] = useState({ incident: '', reason: '', clientKey: null });
  const [busy, setBusy] = useState(false), [loading, setLoading] = useState(false);
  const [error, setError] = useState(null), [notice, setNotice] = useState(null);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    if (!auth.enabled) return;
    let live = true;
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 8000);
    setLoading(true); setError(null);
    loadPolicies(controller.signal).then((value) => { if (live) { setData(value); setForm(draft(value.policy)); } })
      .catch((cause) => { if (live) { setData(null); setError(cause.name === 'AbortError' ? 'Lecture trop longue. Actualisez.' : cause.message); } })
      .finally(() => { clearTimeout(timer); if (live) setLoading(false); });
    return () => { live = false; clearTimeout(timer); controller.abort(); };
  }, [auth.enabled, refreshToken, reload]);
  function change(name, value) { setForm((valueBefore) => ({ ...valueBefore, [name]: value, ...(name === 'mode' ? { confirm_automatic: false } : {}) })); }
  function changeApproval(name, value) { setApproval((before) => ({ ...before, [name]: value, clientKey: null })); }
  async function save(event) {
    event.preventDefault(); setBusy(true); setError(null); setNotice(null);
    try {
      const response = await jsonRequest('/api/v1/policies', { method: 'POST', csrfToken: auth.csrfToken,
        body: policyPayload(data.policy, form), signal: AbortSignal.timeout(12000) });
      const policy = validatePolicy(response?.policy);
      setData((before) => ({ ...before, policy })); setForm(draft(policy));
      setNotice('Politique enregistrée.'); setReload((value) => value + 1);
    } catch (cause) { setError(cause.name === 'TimeoutError' ? 'Réponse trop longue : actualisez pour vérifier l’état enregistré.' : cause.message); }
    finally { setBusy(false); }
  }
  async function approve(event) {
    event.preventDefault(); setBusy(true); setError(null); setNotice(null);
    const clientKey = approval.clientKey || key(); setApproval((before) => ({ ...before, clientKey }));
    try {
      const response = await jsonRequest('/api/v1/policies/approvals', { method: 'POST', csrfToken: auth.csrfToken,
        body: approvalPayload(data.policy, approval.incident, approval.reason, clientKey), signal: AbortSignal.timeout(12000) });
      if (response?.action_dispatched !== false || !response.approval || !Number.isFinite(Date.parse(response.approval.valid_until))) throw new Error('Approbation reçue invalide.');
      setNotice(`Approbation enregistrée jusqu’au ${date(response.approval.valid_until)}. La correction conserve ses vérifications techniques.`);
      setApproval({ incident: '', reason: '', clientKey: null }); setReload((value) => value + 1);
    } catch (cause) { setError(cause.name === 'TimeoutError' ? 'Réponse trop longue : actualisez l’historique avant de réessayer.' : cause.message); }
    finally { setBusy(false); }
  }
  if (!auth.enabled) return null;
  return <section id="policies" className="card panel" aria-labelledby="policies-heading">
    <div className="panel-heading"><div><h2 id="policies-heading">Politiques de pilotage</h2><p>Autonomie, suspension et protection des évaluations.</p></div>
      <button className="refresh-button" disabled={busy || loading} onClick={() => setReload((value) => value + 1)}>Actualiser</button></div>
    <div className="ops-body">{loading && <p role="status">Lecture des politiques…</p>}{error && <p role="alert" className="auth-error">{error}</p>}{notice && <p role="status">{notice}</p>}
      {data && form && <PolicyView data={data} admin={canManageUsers(auth.user)} form={form} change={change} save={save} approve={approve}
        busy={busy || loading} approval={approval} changeApproval={changeApproval} />}</div>
  </section>;
}
