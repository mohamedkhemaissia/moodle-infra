import { useEffect, useState } from 'react';
import { decisionLabels, loadHistory, makeHistoryReport, downloadHistory } from './decisionHistory.js';
import './reports.css';
import './decisionHistory.css';

const date = (value) => value ? new Intl.DateTimeFormat('fr-FR', { dateStyle: 'short', timeStyle: 'medium', timeZone: 'UTC' }).format(Date.parse(value)) + ' UTC' : 'En attente';

export function HistoryView({ value, selectedId, onSelect, now = Date.now() }) {
  const selected = value.items.find((entry) => entry.id === selectedId);
  const coverage = value.coverage;
  const aged = coverage.status === 'running' && now - Math.min(Date.parse(coverage.last_input_at), Date.parse(coverage.last_evaluated_at)) > 90_000;
  const status = aged ? 'stale' : coverage.status;
  return <>
    <p className="ops-caption">Journal activé le {date(value.coverage.started_at)}. Aucun historique reconstruit avant cette date.</p>
    <p className={status === 'running' ? 'ops-caption' : 'catalog-warning'} role="status">
      {status === 'running' ? 'Observateur actif' : status === 'waiting' ? 'Première collecte en attente' : 'Collecte obsolète ou horloge incohérente'}
      {' · Dernière collecte : ' + date(value.coverage.last_input_at)}</p>
    {value.items.length ? <div className="ops-table-wrap"><table className="reports-table"><thead><tr>
      <th>Entrée / incident</th><th>Signal</th><th>Décision</th><th>Première évaluation</th><th>Dernière confirmation</th><th>Preuves</th>
    </tr></thead><tbody>{value.items.map((entry) => <tr key={entry.id}>
      <td>#{entry.id} / #{entry.incident_id}</td><td>{entry.decision.signal_key}</td><td>{decisionLabels[entry.status]}</td>
      <td>{date(entry.first_observed_at)}</td><td>{date(entry.last_observed_at)}<small className="own-account">{entry.observations} observation(s)</small></td>
      <td><button className="refresh-button" aria-expanded={selectedId === entry.id} onClick={() => onSelect(selectedId === entry.id ? null : entry.id)}>Consulter</button></td>
    </tr>)}</tbody></table></div> : <p className="ops-empty">Aucune décision enregistrée pour ce filtre.</p>}
    {selected && <article className="history-detail" aria-label={'Preuves de la décision ' + selected.id}>
      <h3>Décision #{selected.id} · Incident #{selected.incident_id}</h3>
      <p>{selected.decision.explanation}</p><p>Motifs : {selected.decision.reason_codes.join(', ')}</p>
      <p>{selected.decision.recommended_action?.title || 'Aucune action proposée pour cet état.'}</p>
      {selected.decision.recommended_action && <ul>{selected.decision.recommended_action.steps.map((step, index) => <li key={index}>{step}</li>)}</ul>}
      <dl><dt>Date des données</dt><dd>{date(selected.decision.input_evaluated_at)}</dd>
        <dt>Contexte / modèle</dt><dd>{selected.decision.context}</dd>
        <dt>Politique</dt><dd>{selected.decision.policy_version} · {selected.decision.policy_sha256}</dd>
        <dt>Identifiant de décision</dt><dd>{selected.decision.decision_id}</dd></dl>
      <h4>Mesures à la première évaluation</h4><pre>{JSON.stringify(selected.decision.evidence, null, 2)}</pre>
      <details><summary>Politique et état de l’observateur conservés</summary>
        <pre>{JSON.stringify({ policy: selected.policy, collector: selected.collector }, null, 2)}</pre></details>
      <p className="ops-note">Une décision archivée ne permet aucune exécution. Toute action exige une évaluation actuelle.</p>
    </article>}
  </>;
}

export default function DecisionHistory({ refreshToken, now }) {
  const [status, setStatus] = useState('');
  const [incidentInput, setIncidentInput] = useState('');
  const [incidentId, setIncidentId] = useState('');
  const [cursors, setCursors] = useState([null]);
  const [selectedId, setSelectedId] = useState(null);
  const [refresh, setRefresh] = useState(0);
  const [reading, setReading] = useState({ pending: true });
  const [downloadError, setDownloadError] = useState(null);
  const beforeId = cursors.at(-1);
  const key = `${status}:${incidentId}:${beforeId}:${refresh}:${refreshToken}`;
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    setReading({ pending: true }); setSelectedId(null); setDownloadError(null);
    loadHistory({ status, beforeId, incidentId }, controller.signal).then((value) => {
      if (active) setReading({ value, pending: false, key, retrievedAt: Date.now() });
    }).catch((error) => {
      if (active) setReading({ pending: false, error: error.name === 'AbortError' ? 'Le délai de lecture est dépassé.' : error.message, key });
    }).finally(() => clearTimeout(timeout));
    return () => { active = false; clearTimeout(timeout); controller.abort(); };
  }, [status, incidentId, beforeId, refresh, refreshToken]);
  const current = reading.key === key ? reading : { pending: true };
  function download(format) {
    if (!current.value || current.pending) return;
    try { downloadHistory(makeHistoryReport(current.value, { status, beforeId, incidentId }, cursors.length - 1, current.retrievedAt), format); }
    catch { setDownloadError('Le rapport n’a pas pu être préparé.'); }
  }
  return <section id="decision-history" className="panel ops-panel" aria-labelledby="history-heading">
    <div className="panel-heading"><div><h2 id="history-heading">Historique des décisions</h2>
      <p>Recommandations, blocages et clôtures conservés avec leurs preuves.</p></div><span className="subtle-badge">CSV · HTML</span></div>
    <div className="ops-body"><div className="reports-filters">
      <label>État <select value={status} onChange={(event) => { setStatus(event.target.value); setCursors([null]); }}>
        <option value="">Tous</option>{Object.entries(decisionLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <form onSubmit={(event) => { event.preventDefault(); setIncidentId(incidentInput); setCursors([null]); }}>
        <label>Incident <input type="number" min="1" step="1" value={incidentInput} placeholder="Tous"
          onChange={(event) => setIncidentInput(event.target.value)} /></label><button className="refresh-button">Filtrer</button></form>
    </div><div className="reports-actions">
      <button className="refresh-button" disabled={current.pending} onClick={() => { setCursors([null]); setRefresh((value) => value + 1); }}>Actualiser le journal</button>
      <button className="ops-button" disabled={current.pending || !current.value} onClick={() => download('csv')}>Exporter la page CSV</button>
      <button className="ops-button" disabled={current.pending || !current.value} onClick={() => download('html')}>Rapport HTML</button>
    </div>{downloadError && <p role="alert" className="catalog-warning">{downloadError}</p>}
    {current.pending ? <p role="status">Lecture du journal…</p> : current.error ? <p role="alert" className="catalog-warning">{current.error}</p> : <>
      <HistoryView value={current.value} selectedId={selectedId} onSelect={setSelectedId} now={now} />
      <div className="ops-pagination"><span>Page {cursors.length} · Les exports incluent cette page.</span><div>
        <button disabled={cursors.length === 1} onClick={() => setCursors((values) => values.slice(0, -1))}>Précédente</button>
        <button disabled={!current.value.has_more} onClick={() => setCursors((values) => [...values, current.value.next_before_id])}>Suivante</button>
      </div></div></>}
    </div>
  </section>;
}
