import { useEffect, useState } from "react";
import { useAuth } from "./Auth.jsx";
import { canManageUsers } from "./authClient.js";
import { createDiagnosticRequest, listDiagnosticRequests, requestKey, diagnosticStates } from "./diagnosticRequests.js";

export default function DiagnosticRequestPanel({ incidentId }) {
  const auth = useAuth();
  const [reason, setReason] = useState("");
  const [clientKey, setClientKey] = useState(requestKey);
  const [reading, setReading] = useState({ loading: true, items: [], error: null });
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setReading({ loading: true, items: [], error: null });
    listDiagnosticRequests(incidentId, controller.signal)
      .then((value) => { if (active) setReading({ loading: false, items: value.items, error: null }); })
      .catch((error) => { if (active) setReading({ loading: false, items: [], error: error.status === 503 ? "Les demandes ne sont pas encore activees sur le serveur." : error.message }); });
    return () => { active = false; controller.abort(); };
  }, [incidentId, revision]);

  async function submit(event) {
    event.preventDefault();
    if (busy || reading.loading || reading.error) return;
    setBusy(true);
    setMessage("");
    try {
      await createDiagnosticRequest({ incidentId, reason, clientKey, csrfToken: auth.csrfToken });
      setReason("");
      setClientKey(requestKey());
      setMessage("Demande enregistree. Elle attend son traitement par le worker.");
      setRevision((value) => value + 1);
    } catch (error) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  const available = !reading.loading && !reading.error;
  return <section className="decision-card" aria-label={`Demandes de diagnostic pour incident ${incidentId}`}>
    <h4>Diagnostic en lecture seule</h4>
    <p className="ops-caption">Les demandes sont journalisees. Leur enregistrement ne lance aucune correction.</p>
    {reading.loading && <p role="status">Chargement des demandes...</p>}
    {reading.error && <p className="ops-note" role="status">{reading.error}</p>}
    {available && (reading.items.length ? <ul className="audit-list">{reading.items.map((item) => <li key={item.id}><strong>{diagnosticStates[item.status]}</strong> · {item.requested_by} · {new Date(item.created_at * 1000).toLocaleString("fr-FR")} · {item.reason}</li>)}</ul> : <p className="ops-caption">Aucune demande pour cet incident.</p>)}
    {canManageUsers(auth.user) && available && <form className="auth-form" onSubmit={submit}>
      <label>Motif du diagnostic<textarea required minLength={15} maxLength={500} rows={3} value={reason} disabled={busy} onChange={(event) => { setReason(event.target.value); setClientKey(requestKey()); }} placeholder="Expliquez les observations a examiner" /></label>
      <button className="refresh-button" type="submit" disabled={busy || reason.trim().length < 15}>{busy ? "Enregistrement..." : "Demander un diagnostic"}</button>
    </form>}
    {message && <p role="status" className="ops-note">{message}</p>}
  </section>;
}
