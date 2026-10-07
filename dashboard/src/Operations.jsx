import { useState } from 'react';
import { RESOURCES, analysisReady, decisionCurrent, recoveryProven, requestPath } from './operations.js';
import { useReadings } from './useReadings.js';

const numbers = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 2 });
const dates = new Intl.DateTimeFormat('fr-FR', { dateStyle: 'short', timeStyle: 'medium' });
const date = (value) => {
  const milliseconds = typeof value === 'number' ? value * 1000 : Date.parse(value);
  return Number.isFinite(milliseconds) && Math.abs(milliseconds) <= 8.64e15 ? dates.format(milliseconds) : '—';
};
const labels = { open: 'Ouvert', resolved: 'Résolu', completed: 'Terminé', blocked: 'Bloqué', failed: 'Échec',
  running: 'En cours', timeout: 'Délai dépassé', canceled: 'Annulé', interrupted: 'Interrompu',
  recommendation: 'Recommandation', monitoring: 'Sous observation', closed: 'Incident résolu',
  warning: 'Avertissement', critical: 'Critique', info: 'Information' };
const reasons = { insufficient_contiguous_history: 'Historique continu insuffisant pour calculer l’analyse.',
  linux_collection_unavailable: 'La collecte Linux ne fournit pas de données récentes.',
  incomplete_or_unhealthy_collection: 'Des mesures sont absentes, anciennes ou issues d’une collecte en échec.' };

function Badge({ state }) {
  const tone = ['completed', 'resolved'].includes(state) ? 'good' :
    ['failed', 'critical', 'timeout'].includes(state) ? 'bad' :
    ['blocked', 'warning', 'open'].includes(state) ? 'warning' : 'neutral';
  return <span className={`ops-badge ${tone}`}>{labels[state] || 'État inconnu'}</span>;
}

function Reading({ result, children, empty = 'Aucune donnée disponible.' }) {
  if (result.pending) return <p className="ops-empty" role="status">Lecture en cours…</p>;
  if (!result.data) return <p className="ops-empty" role="status">{result.error ||
    (result.stale ? 'Cette lecture est ancienne. Actualiser pour obtenir des données récentes.' : empty)}</p>;
  return children(result.data);
}

function Pager({ document, offset, setOffset, name }) {
  return <div className="ops-pagination" aria-label={`Pagination ${name}`}>
    <span>{document.total} résultat{document.total !== 1 ? 's' : ''} · page {offset / 10 + 1}</span>
    <div><button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 10))}>Précédente</button>
      <button disabled={!document.has_more || offset >= 100000} onClick={() => setOffset(offset + 10)}>Suivante</button></div>
  </div>;
}

function Forecasts({ result, now }) {
  return <article className="ops-card"><h3>Prévisions des ressources</h3>
    <p className="ops-caption">Estimations à 5, 10 et 15 minutes · pourcentages</p>
    <Reading result={result}>{(data) => analysisReady(data, now) ? <>
      <div className="ops-table-wrap"><table><thead><tr><th>Ressource</th>
        {['5', '10', '15'].map((h) => <th key={h}>+{h} min</th>)}</tr></thead>
        <tbody>{RESOURCES.map(([key, title]) => <tr key={key}><th>{title}</th>
          {['5', '10', '15'].map((h) => { const value = data.forecasts[h].metrics[key];
            return <td key={h}><strong>{numbers.format(value.value)} %</strong><small>
              {value.method === 'ridge' ? 'Régression Ridge' : 'Persistance'}</small></td>; })}</tr>)}</tbody></table></div>
      <p className="ops-caption">Données du {date(data.input_evaluated_at)}. Méthode choisie sur la validation historique.</p>
    </> : <p className="ops-empty">{reasons[data.reason] || 'Analyse indisponible avec les données actuelles.'}
      {data.required_history_seconds ? ` Historique requis : ${data.required_history_seconds / 60} minutes.` : ''}</p>}</Reading>
  </article>;
}

function Anomalies({ result, now }) {
  return <article className="ops-card"><h3>Détection des anomalies</h3>
    <p className="ops-caption">Isolation Forest · CPU, mémoire et disque</p>
    <Reading result={result}>{(data) => analysisReady(data, now) ? <>
      <div className="anomaly-score"><strong>{numbers.format(data.anomaly.score)}</strong><span>
        Seuil : {numbers.format(data.anomaly.threshold)}</span></div>
      <p className="anomaly-verdict">{data.anomaly.statistical_anomaly ? 'Observation atypique détectée' : 'Observation dans le seuil statistique'}</p>
      <p className="ops-caption">Seuils opérationnels : {data.anomaly.operational_rules.alert ? 'alerte détectée' : 'aucun dépassement'}.</p>
      <p className="ops-caption">Données du {date(data.input_evaluated_at)}.</p>
    </> : <p className="ops-empty">{reasons[data.reason] || 'Analyse indisponible avec les données actuelles.'}</p>}</Reading>
    <p className="ops-note">Le score indique une observation atypique. La qualité de détection reste à évaluer avec des incidents étiquetés.</p>
  </article>;
}

function RunList({ title, kind, result, offset, setOffset, select, selected }) {
  return <article className="ops-card audit-card"><h3>{title}</h3>
    <p className="ops-caption">Journal des exécutions enregistrées</p>
    <Reading result={result}>{(data) => <>
      {data.items.length ? <ul className="audit-list">{data.items.map((item) =>
        <li key={item.id}><button className={`audit-row ${selected === item.id ? 'selected' : ''}`}
          onClick={() => select({ kind, id: item.id })} aria-label={`Consulter ${title.toLowerCase()} ${item.id.slice(0, 8)}`}>
          <span><strong>Incident #{item.incident_id}</strong><small>{date(item.started_at)}</small>
            <code>{item.id.slice(0, 8)}</code></span><Badge state={item.status} /></button></li>)}</ul>
        : <p className="ops-empty">Aucune exécution enregistrée pour cette sélection.</p>}
      <Pager document={data} offset={offset} setOffset={setOffset} name={title} />
    </>}</Reading>
  </article>;
}

export default function Operations({ now, refreshToken }) {
  const [filter, setFilter] = useState('');
  const [offset, setOffset] = useState(0);
  const [incidentId, setIncidentId] = useState(null);
  const [diagnosticOffset, setDiagnosticOffset] = useState(0);
  const [remediationOffset, setRemediationOffset] = useState(0);
  const [run, setRun] = useState(null);
  const [localRefresh, setLocalRefresh] = useState(0);
  const requests = [
    { key: 'forecast', kind: 'forecast', path: requestPath('forecast') },
    { key: 'anomaly', kind: 'anomaly', path: requestPath('anomaly') },
    { key: 'summary', kind: 'summary', path: requestPath('summary') },
    { key: 'incidents', kind: 'incidents', path: requestPath('incidents', { status: filter, offset }) },
    { key: 'diagnostics', kind: 'runs', path: requestPath('diagnostics', { incidentId, offset: diagnosticOffset }) },
    { key: 'remediations', kind: 'runs', path: requestPath('remediations', { incidentId, offset: remediationOffset }) },
  ];
  if (incidentId !== null) requests.push({ key: 'decision', kind: 'decision', path: requestPath('decision', { incidentId }) });
  if (run) requests.push({ key: 'run', kind: 'run', path: requestPath(run.kind, { runId: run.id }) });
  const readings = useReadings(requests, `${refreshToken}:${localRefresh}`, now);
  function selectIncident(id) {
    setIncidentId(id); setDiagnosticOffset(0); setRemediationOffset(0); setRun(null);
  }
  const selectedIncident = readings.incidents.data?.items.find((item) => item.id === incidentId);
  const collector = readings.summary.data?.collector;
  const collectorAge = collector ? Math.max(collector.age_seconds, collector.input_age_seconds) +
    Math.max(0, (now - Date.parse(readings.summary.data.evaluated_at)) / 1000) : null;
  const collectorReady = collector?.status === 'running' && Number.isFinite(collectorAge) && collectorAge <= 90;

  return <>
    <section id="analysis" className="panel ops-panel" aria-labelledby="analysis-heading">
      <div className="panel-heading"><div><h2 id="analysis-heading">Analyse par intelligence artificielle</h2>
        <p>Résultats expérimentaux calculés à partir de l’historique des ressources.</p></div>
        <span className="subtle-badge">Mode observation</span></div>
      <div className="ops-grid"><Forecasts result={readings.forecast} now={now} /><Anomalies result={readings.anomaly} now={now} /></div>
      <p className="ops-section-note">Les estimations restent expérimentales et ne déclenchent aucune action.</p>
    </section>

    <section id="incidents" className="panel ops-panel" aria-labelledby="incidents-heading">
      <div className="panel-heading"><div><h2 id="incidents-heading">Incidents et recommandations</h2>
        <p>Suivre les épisodes détectés et consulter le diagnostic proposé.</p></div>
        <button className="ops-button" onClick={() => setLocalRefresh((n) => n + 1)}>Actualiser les incidents</button></div>
      <div className="ops-body">
        <Reading result={readings.summary}>{(data) => <div className="incident-summary">
          <span><strong>{data.counts.open}</strong> ouverts</span><span><strong>{data.counts.resolved}</strong> résolus</span>
          <span className={`collector-label ${collectorReady ? 'good' : 'warning'}`}>
            {collectorReady ? 'Observateur actif' : 'Observateur absent ou données anciennes'}</span>
        </div>}</Reading>
        <div className="ops-toolbar"><label>Épisodes <select value={filter} onChange={(e) => {
          setFilter(e.target.value); setOffset(0); selectIncident(null);
        }}><option value="">Tous</option><option value="open">Ouverts</option><option value="resolved">Résolus</option></select></label>
          <span className="ops-caption">Actualisation automatique · 30 s</span></div>
        <Reading result={readings.incidents}>{(data) => <>
          {data.items.length ? <div className="ops-table-wrap"><table className="incident-table"><thead><tr>
            <th>Incident</th><th>État</th><th>Ouverture</th><th>Dernière observation</th><th>Résolution</th><th>Lecture</th>
          </tr></thead><tbody>{data.items.map((item) => <tr key={item.id} className={incidentId === item.id ? 'selected' : ''}>
            <td><strong>#{item.id} · {item.title}</strong><small>{labels[item.severity] || item.severity}</small></td>
            <td><Badge state={item.status} /></td><td>{date(item.opened_at)}</td><td>{date(item.last_observed_at)}</td>
            <td>{date(item.resolved_at)}</td><td><button className="ops-button" onClick={() => selectIncident(item.id)}
              aria-label={`Consulter incident ${item.id}`}>Consulter</button></td>
          </tr>)}</tbody></table></div> : <p className="ops-empty">Aucun incident pour ce filtre.</p>}
          <Pager document={data} offset={offset} setOffset={(value) => { setOffset(value); selectIncident(null); }} name="incidents" />
        </>}</Reading>
        {incidentId !== null && <article className="decision-card" aria-label={`Décision incident ${incidentId}`}>
          <div className="ops-toolbar"><h3>Incident #{incidentId}{selectedIncident ? ` · ${selectedIncident.title}` : ''}</h3>
            <button className="ops-button" onClick={() => selectIncident(null)}>Voir tous les journaux</button></div>
          <Reading result={readings.decision}>{(data) => { const decision = data.decision;
            const current = decisionCurrent(data, now);
            return <><Badge state={decision.status === 'recommendation' && !current ? 'blocked' : decision.status} />
              {decision.status === 'recommendation' && !current ? <p className="ops-empty">Recommandation expirée ou données indisponibles. Une nouvelle évaluation est nécessaire.</p>
                : <><p className="decision-explanation">{decision.explanation}</p>
                  {decision.recommended_action && <><h4>{decision.recommended_action.title}</h4><ol>
                    {decision.recommended_action.steps.map((step, index) => <li key={index}>{step}</li>)}</ol></>}</>}
              <p className="ops-caption">Politique {decision.policy_version} · évaluation {date(data.evaluated_at)}
                {decision.valid_until ? ` · validité jusqu’à ${date(decision.valid_until)}` : ''}</p>
              <details><summary>Motifs et preuves du signal</summary><pre>{JSON.stringify({ reasons: decision.reason_codes, evidence: decision.evidence }, null, 2)}</pre></details>
            </>; }}</Reading>
          <p className="ops-note">Les recommandations sont calculées en simulation. Les journaux ci-dessous retracent les exécutions réellement enregistrées.</p>
        </article>}
      </div>
    </section>

    <section id="audits" className="panel ops-panel" aria-labelledby="audits-heading">
      <div className="panel-heading"><div><h2 id="audits-heading">Diagnostics et remédiations</h2>
        <p>{incidentId === null ? 'Historique de tous les incidents.' : `Historique de l’incident #${incidentId}.`}</p></div>
        <span className="subtle-badge">Consultation</span></div>
      <div className="ops-grid"><RunList title="Diagnostics" kind="diagnostics" result={readings.diagnostics}
        offset={diagnosticOffset} setOffset={setDiagnosticOffset} select={setRun} selected={run?.kind === 'diagnostics' ? run.id : null} />
        <RunList title="Remédiations" kind="remediations" result={readings.remediations}
          offset={remediationOffset} setOffset={setRemediationOffset} select={setRun} selected={run?.kind === 'remediations' ? run.id : null} /></div>
      {run && <div className="ops-body run-detail" aria-label="Détail de l’exécution"><Reading result={readings.run}>{(data) => <>
        <div className="ops-toolbar"><h3>{run.kind === 'remediations' ? 'Remédiation' : 'Diagnostic'} · incident #{data.incident_id}</h3>
          <Badge state={data.status} /></div>
        <p className="ops-caption">Exécution {data.id} · terminée le {date(data.finished_at)}</p>
        {run.kind === 'remediations' ? <p className={`run-verdict ${recoveryProven(data) ? 'good' : 'warning'}`}>
          {recoveryProven(data) ? 'Récupération confirmée par HTTPS et une nouvelle sonde Prometheus.' :
            data.status === 'blocked' ? 'Action bloquée par les contrôles de remédiation.' : 'Récupération non confirmée dans ce journal.'}</p>
          : <p className="ops-note">Le diagnostic collecte des preuves en lecture seule. Son achèvement ne confirme pas à lui seul la santé du service.</p>}
        {data.report?.reason && <p className="ops-caption">Motif enregistré : {data.report.reason}</p>}
        <details><summary>Consulter le rapport et la trace d’exécution</summary><pre>{JSON.stringify({
          report: data.report, events: data.events, action_reservation: data.action_reservation }, null, 2)}</pre></details>
        <a className="ops-json-link" href={requestPath(run.kind, { runId: data.id })} target="_blank" rel="noreferrer">Ouvrir le journal JSON ↗</a>
      </>}</Reading></div>}
      <p className="ops-section-note">La remédiation est lancée manuellement par l’opérateur sur le serveur. Le tableau de bord consulte son résultat.</p>
    </section>
  </>;
}
