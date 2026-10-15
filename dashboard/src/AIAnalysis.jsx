import { RESOURCES, analysisReady } from './operations.js';
import { DetailsPanel, StatusBadge } from './ApplicationServices.jsx';

const numbers = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 2 });
const scores = new Intl.NumberFormat('fr-FR', { minimumFractionDigits: 3, maximumFractionDigits: 3 });
const dates = new Intl.DateTimeFormat('fr-FR', { dateStyle: 'short', timeStyle: 'medium' });
const reasons = {
  insufficient_contiguous_history: 'L’historique continu est encore insuffisant pour cette analyse.',
  linux_collection_unavailable: 'La collecte des ressources ne fournit pas de mesures récentes.',
  incomplete_or_unhealthy_collection: 'Certaines mesures sont absentes, anciennes ou issues d’une collecte interrompue.',
};
const date = (value) => {
  const time = Date.parse(value);
  return Number.isFinite(time) && Math.abs(time) <= 8.64e15 ? dates.format(time) : 'Non communiquée';
};
const scoreValid = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1;
const percent = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100;
const resourceNames = {
  cpu_usage_percent: ['CPU', 'Processeur'], memory_usage_percent: ['RAM', 'Mémoire'],
  disk_available_percent: ['SSD', 'Disque disponible'],
};
const percentage = (value) => percent(value) ? `${numbers.format(value)} %` : '—';
const trend = (current, future) => !percent(current) || !percent(future) ? 'Indisponible' :
  Math.abs(future - current) < 1 ? 'Stable' : future > current ? 'En hausse' : 'En baisse';

function Unavailable({ result = {}, data }) {
  const message = result.pending ? 'Lecture de l’analyse…' : result.stale
    ? 'Les données sont anciennes. Actualisez pour obtenir une analyse récente.' : result.error
      ? 'Le service d’analyse est indisponible. Actualisez pour réessayer.'
      : reasons[data?.reason] || 'L’analyse n’est pas disponible avec les observations actuelles.';
  return <div className="ai-unavailable" role="status"><strong>Analyse temporairement indisponible</strong><p>{message}
    {!result.pending && typeof data?.required_history_seconds === 'number' && data.required_history_seconds > 0
      && ` Historique continu nécessaire : ${Math.ceil(data.required_history_seconds / 60)} minutes.`}</p></div>;
}

export function Anomalies({ result = {}, now, onNavigate }) {
  const data = result.data;
  const anomaly = data?.anomaly;
  const ready = !result.pending && !result.error && !result.stale && analysisReady(data, now)
    && scoreValid(anomaly?.score) && scoreValid(anomaly?.threshold)
    && typeof anomaly.statistical_anomaly === 'boolean'
    && anomaly.statistical_anomaly === (anomaly.score > anomaly.threshold);
  const atypical = ready && anomaly.statistical_anomaly;
  return <article className="ai-analysis-card ai-cockpit" aria-labelledby="anomaly-heading">
    <div className="overview-heading"><div><p className="eyebrow">Assistant IA</p>
      <h3 id="anomaly-heading">Surveillance intelligente des ressources</h3></div>
      <StatusBadge state={ready ? atypical ? 'warning' : 'operational' : 'unknown'}
        label={ready ? atypical ? 'À vérifier' : 'Situation normale' : 'Indisponible'} /></div>
    {ready ? <>
      <section className={`ai-state-banner ${atypical ? 'atypical' : 'usual'}`} aria-label="Conclusion de l’analyse">
        <span className="ai-state-icon" aria-hidden="true">{atypical ? '!' : '✓'}</span><div>
          <h4>{atypical ? 'Variation inhabituelle détectée' : 'Activité habituelle'}</h4>
          <p>{atypical ? 'Une évolution inhabituelle des ressources demande une vérification.' :
            'Aucun comportement inhabituel n’est détecté sur le processeur, la mémoire ou le disque.'}</p>
          <time dateTime={data.input_evaluated_at}>Dernière analyse : {date(data.input_evaluated_at)}</time></div>
      </section>
      <div className="ai-resource-grid" aria-label="Ressources analysées">{RESOURCES.map(([key]) => {
        const [icon, label] = resourceNames[key]; const value = data.current_metrics?.[key];
        return <article className="ai-resource-card" key={key}><span aria-hidden="true">{icon}</span><div>
          <h4>{label}</h4><strong>{percentage(value)}</strong></div></article>;
      })}</div>
      <section className={`ai-action-card ${atypical ? 'atypical' : 'usual'}`}><div>
        <h4>{atypical ? 'Une vérification est recommandée' : 'Aucune intervention nécessaire'}</h4>
        <p>{atypical ? 'Consultez les incidents et confirmez les mesures avant toute intervention.' :
          'Continuez la surveillance habituelle de la plateforme.'}</p></div>
        {atypical && onNavigate && <button className="ops-button" type="button" onClick={() => onNavigate('incidents')}>
          Examiner les incidents</button>}</section>
      <DetailsPanel title="Afficher les détails du modèle" className="ai-technical-details">
        <dl className="summary-facts"><div><dt>Score d’anomalie</dt><dd>{scores.format(anomaly.score)}</dd></div>
          <div><dt>Seuil de détection</dt><dd>{scores.format(anomaly.threshold)}</dd></div>
          <div><dt>Algorithme</dt><dd>Isolation Forest</dd></div></dl>
        <p>Le score décrit un comportement inhabituel. Il ne représente pas une probabilité de panne.</p>
      </DetailsPanel>
    </> : <Unavailable result={result} data={data} />}
  </article>;
}

export function Forecasts({ result = {}, now }) {
  const data = result.data;
  const ready = !result.pending && !result.error && !result.stale && analysisReady(data, now);
  const current = data?.current_metrics || {};
  const stable = ready && RESOURCES.every(([key]) => percent(current[key]) &&
    Math.abs(data.forecasts['15'].metrics[key].value - current[key]) < 1);
  return <article className="forecast-card ai-analysis-card" aria-labelledby="forecast-heading">
    <div className="overview-heading"><h3 id="forecast-heading">Prévisions des ressources</h3>
      <StatusBadge state={ready ? 'operational' : 'unknown'} label={ready ? 'Prévisions disponibles' : 'Analyse indisponible'} /></div>
    {ready ? <>
      <p className="forecast-summary">{stable ? 'Tendance globalement stable' : 'Évolution attendue des ressources'}</p>
      <div className="forecast-timeline" aria-label="Prévisions des ressources à 5, 10 et 15 minutes">
        <div className="forecast-time-heading"><span>Ressource</span><span>Maintenant</span><span>+5 min</span><span>+10 min</span><span>+15 min</span></div>
        {RESOURCES.map(([key]) => <article className={`forecast-track ${key}`} key={key}>
          <div><strong>{resourceNames[key][1]}</strong><small>{trend(current[key], data.forecasts['15'].metrics[key].value)}</small></div>
          <span>{percentage(current[key])}</span>{['5', '10', '15'].map((h) =>
            <span key={h}>{percentage(data.forecasts[h].metrics[key].value)}</span>)}</article>)}
      </div>
      <p className="ai-disclaimer">Prévisions expérimentales d’aide à la surveillance. Elles ne garantissent pas une panne et ne déclenchent aucune intervention.</p>
    </> : <Unavailable result={result} data={data} />}
  </article>;
}

export function AnalysisView({ anomaly, forecast, now, onNavigate }) {
  return <div className="ai-analysis-view"><Anomalies result={anomaly} now={now} onNavigate={onNavigate} />
    <Forecasts result={forecast} now={now} /></div>;
}
