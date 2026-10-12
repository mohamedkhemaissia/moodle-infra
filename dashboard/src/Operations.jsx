import { useEffect, useState } from 'react';
import { RESOURCES, analysisReady, decisionCurrent, recoveryProven, operationRequests } from './operations.js';
import { useReadings } from './useReadings.js';
import { CompactTable, DetailsPanel, StatusBadge as Badge } from './ApplicationServices.jsx';
import { observerCurrent } from './navigation.js';
import { EvidenceSummary, RunSummary, ForecastMethods } from './ReadableDetails.jsx';

const numbers = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 2 });
const dates = new Intl.DateTimeFormat('fr-FR', { dateStyle: 'short', timeStyle: 'medium' });
const date = (value) => {
  const milliseconds = typeof value === 'number' ? value * 1000 : Date.parse(value);
  return Number.isFinite(milliseconds) && Math.abs(milliseconds) <= 8.64e15 ? dates.format(milliseconds) : '—';
};
const reasons = { insufficient_contiguous_history: 'Historique continu insuffisant pour calculer les prévisions.',
  linux_collection_unavailable: 'La collecte Linux ne fournit pas de données récentes.',
  incomplete_or_unhealthy_collection: 'Des mesures sont absentes, anciennes ou issues d’une collecte en échec.' };

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

export function Forecasts({ result, now }) {
  return <article className="forecast-card" aria-label="Prévisions des ressources">
    <Reading result={result}>{(data) => analysisReady(data, now) ? <>
      <CompactTable caption="Prévisions des ressources à 5, 10 et 15 minutes" rows={RESOURCES} rowKey={([key]) => key}
        columns={[{ key: 'resource', label: 'Ressource', render: ([, title]) => title },
          ...['5', '10', '15'].map((h) => ({ key: h, label: '+' + h + ' min',
            render: ([key]) => <strong>{numbers.format(data.forecasts[h].metrics[key].value)} %</strong> }))]} />
      <DetailsPanel title="Méthodes et données de prévision"><p className="ops-caption">Données du {date(data.input_evaluated_at)}.</p>
        <ForecastMethods forecasts={data.forecasts} /></DetailsPanel>
    </> : <p className="ops-empty">{reasons[data.reason] || 'Analyse indisponible avec les données actuelles.'}
      {data.required_history_seconds ? ` Historique requis : ${data.required_history_seconds / 60} minutes.` : ''}</p>}</Reading>
  </article>;
}

export function IncidentTable({ items, selectedId, onSelect }) {
  return <CompactTable caption="Incidents Moodle" rows={items} rowKey={(item) => item.id} selectedKey={selectedId} columns={[
    { key: 'incident', label: 'Incident', render: (item) => <strong>#{item.id} · {item.title}</strong> },
    { key: 'severity', label: 'Gravité', render: (item) => <Badge state={item.severity} /> },
    { key: 'status', label: 'Statut', render: (item) => <Badge state={item.status} /> },
    { key: 'date', label: 'Ouverture', render: (item) => date(item.opened_at) },
    { key: 'action', label: 'Recommandation', render: (item) => <button className="ops-button" aria-label={`Consulter incident ${item.id}`}
      aria-expanded={selectedId === item.id} onClick={() => onSelect(item.id)}>Consulter</button> },
  ]} />;
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

export default function Operations({ now, refreshToken, section = 'incidents', incidentFocus = null, onNavigate }) {
  const [filter, setFilter] = useState('open');
  const [offset, setOffset] = useState(0);
  const [incidentId, setIncidentId] = useState(incidentFocus);
  const [diagnosticOffset, setDiagnosticOffset] = useState(0);
  const [remediationOffset, setRemediationOffset] = useState(0);
  const [run, setRun] = useState(null);
  const [localRefresh, setLocalRefresh] = useState(0);
  const requests = operationRequests(section, { status: filter, offset, incidentId,
    diagnosticOffset, remediationOffset, run });
  const readings = useReadings(requests, `${refreshToken}:${localRefresh}`, now);
  function selectIncident(id) {
    setIncidentId(id); setDiagnosticOffset(0); setRemediationOffset(0); setRun(null);
  }
  useEffect(() => { if (incidentFocus !== null) selectIncident(incidentFocus); }, [incidentFocus]);
  const selectedIncident = readings.incidents?.data?.items.find((item) => item.id === incidentId);
  const collectorReady = observerCurrent(readings.summary?.data, now);

  return <>
    {section === 'analysis' && <section id="analysis" className="panel ops-panel" aria-labelledby="analysis-heading">
      <div className="panel-heading"><div><h2 id="analysis-heading">Prévisions des ressources</h2>
        <p>Estimation de la consommation à 5, 10 et 15 minutes.</p></div>
        <span className="subtle-badge">Expérimental</span></div>
      <div className="forecast-body"><Forecasts result={readings.forecast} now={now} /></div>
      <p className="ops-section-note">Les prévisions sont expérimentales.</p>
    </section>}

    {section === 'incidents' && <section id="incidents" className="panel ops-panel" aria-labelledby="incidents-heading">
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
          </div>
        <Reading result={readings.incidents}>{(data) => <>
          {data.items.length ? <IncidentTable items={data.items} selectedId={incidentId} onSelect={selectIncident} /> : <p className="ops-empty">Aucun incident pour ce filtre.</p>}
          <Pager document={data} offset={offset} setOffset={(value) => { setOffset(value); selectIncident(null); }} name="incidents" />
        </>}</Reading>
        {incidentId !== null && <article className="decision-card" aria-label={`Décision incident ${incidentId}`}>
          <div className="ops-toolbar"><h3>Incident #{incidentId}{selectedIncident ? ` · ${selectedIncident.title}` : ''}</h3>
            <button className="ops-button" onClick={() => selectIncident(null)}>Fermer le détail</button>
            {onNavigate && <button className="ops-button" onClick={() => onNavigate('audits')}>Voir les journaux</button>}</div>
          <Reading result={readings.decision}>{(data) => { const decision = data.decision;
            const current = decisionCurrent(data, now);
            return <><Badge state={decision.status === 'recommendation' && !current ? 'blocked' : decision.status} />
              {decision.status === 'recommendation' && !current ? <p className="ops-empty">Recommandation expirée ou données indisponibles. Une nouvelle évaluation est nécessaire.</p>
                : <><p className="decision-explanation">{decision.explanation}</p>
                  {decision.recommended_action && <><h4>{decision.recommended_action.title}</h4><ol>
                    {decision.recommended_action.steps.map((step, index) => <li key={index}>{step}</li>)}</ol></>}</>}
              <DetailsPanel title="Chronologie, politique et preuves du signal">
                {selectedIncident && <dl className="summary-facts"><div><dt>Dernière observation</dt><dd>{date(selectedIncident.last_observed_at)}</dd></div>
                  <div><dt>Résolution</dt><dd>{date(selectedIncident.resolved_at)}</dd></div></dl>}
                <p className="ops-caption">Politique {decision.policy_version} · évaluation {date(data.evaluated_at)}
                  {decision.valid_until ? ` · validité jusqu’à ${date(decision.valid_until)}` : ''}</p>
                <EvidenceSummary evidence={decision.evidence} />
              </DetailsPanel>
            </>; }}</Reading>
          <p className="ops-note">Les recommandations sont calculées en simulation. L’historique des exécutions retrace les opérations réellement enregistrées.</p>
        </article>}
      </div>
    </section>}

    {section === 'audits' && <section id="audits" className="panel ops-panel" aria-labelledby="audits-heading">
      <div className="panel-heading"><div><h2 id="audits-heading">Diagnostics et remédiations</h2>
        <p>{incidentId === null ? 'Historique de tous les incidents.' : `Historique de l’incident #${incidentId}.`}</p></div>
        {incidentId !== null ? <button className="ops-button" onClick={() => selectIncident(null)}>Tous les journaux</button>
          : <span className="subtle-badge">Consultation</span>}</div>
      <div className="ops-grid"><RunList title="Diagnostics" kind="diagnostics" result={readings.diagnostics}
        offset={diagnosticOffset} setOffset={setDiagnosticOffset} select={setRun} selected={run?.kind === 'diagnostics' ? run.id : null} />
        <RunList title="Remédiations" kind="remediations" result={readings.remediations}
          offset={remediationOffset} setOffset={setRemediationOffset} select={setRun} selected={run?.kind === 'remediations' ? run.id : null} /></div>
      {run && <div className="ops-body run-detail" aria-label="Détail de l’exécution"><Reading result={readings.run}>{(data) => <>
        <div className="ops-toolbar"><h3>{run.kind === 'remediations' ? 'Remédiation' : 'Diagnostic'} · incident #{data.incident_id}</h3>
          <Badge state={data.status} /></div>
        <p className="ops-caption">Terminée le {date(data.finished_at)}</p>
        {run.kind === 'remediations' ? <p className={`run-verdict ${recoveryProven(data) ? 'good' : 'warning'}`}>
          {recoveryProven(data) ? 'Correction confirmée par les contrôles HTTPS et les nouvelles mesures.' :
            data.status === 'blocked' ? 'Action bloquée par les contrôles de remédiation.' : 'Récupération non confirmée dans ce journal.'}</p>
          : <p className="ops-note">Diagnostic terminé : consultez le rapport pour connaître les résultats des contrôles.</p>}
        <DetailsPanel title="Résultat et contrôles de l’intervention">
          <RunSummary run={data} />
        </DetailsPanel>
      </>}</Reading></div>}
      <p className="ops-section-note">Interventions enregistrées selon le mode de pilotage actif.</p>
    </section>}
  </>;
}
