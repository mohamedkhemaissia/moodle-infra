import { RESOURCES, analysisReady } from './operations.js';
import { CompactTable, DetailsPanel, StatusBadge } from './ApplicationServices.jsx';
import { SummaryList } from './ReadableDetails.jsx';

const numbers = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 2 });
const scores = new Intl.NumberFormat('fr-FR', { minimumFractionDigits: 3, maximumFractionDigits: 3 });
const dates = new Intl.DateTimeFormat('fr-FR', { dateStyle: 'short', timeStyle: 'medium' });
const methods = { ridge: 'Régression Ridge', persistence: 'Maintien de la dernière valeur' };
const reasons = {
  insufficient_contiguous_history: 'L’historique continu est encore insuffisant pour cette analyse.',
  linux_collection_unavailable: 'La collecte des ressources ne fournit pas de mesures récentes.',
  incomplete_or_unhealthy_collection: 'Certaines mesures sont absentes, anciennes ou issues d’une collecte interrompue.',
};
const date = (value) => {
  const time = Date.parse(value);
  return Number.isFinite(time) && Math.abs(time) <= 8.64e15 ? dates.format(time) : 'Non communiquée';
};
const reference = (value) => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
  ? value.slice(0, 12) : 'Non communiquée';
const scoreValid = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1;

function Unavailable({ result = {}, data }) {
  const message = result.pending ? 'Lecture de l’analyse…' : result.stale
    ? 'Les données sont anciennes. Actualisez pour obtenir une analyse récente.' : result.error
      ? 'Le service d’analyse est indisponible. Actualisez pour réessayer.'
      : reasons[data?.reason] || 'L’analyse n’est pas disponible avec les observations actuelles.';
  return <p className="ops-empty" role="status">{message}
    {!result.pending && typeof data?.required_history_seconds === 'number' && data.required_history_seconds > 0
      && ` Historique continu nécessaire : ${Math.ceil(data.required_history_seconds / 60)} minutes.`}</p>;
}

function ModelFacts({ data, name }) {
  return <SummaryList rows={[
    ['Modèle', name],
    ['Version · référence courte', reference(data.model_sha256 || data.model_id)],
    ['Création du modèle', date(data.model_created_at)],
    ['Observations utilisées', date(data.input_evaluated_at)],
    ['Lecture de l’analyse', date(data.evaluated_at)],
  ]} />;
}

export function Anomalies({ result = {}, now }) {
  const data = result.data;
  const anomaly = data?.anomaly;
  const ready = !result.pending && !result.error && !result.stale && analysisReady(data, now)
    && scoreValid(anomaly?.score) && scoreValid(anomaly?.threshold)
    && typeof anomaly.statistical_anomaly === 'boolean'
    && anomaly.statistical_anomaly === (anomaly.score > anomaly.threshold);
  const atypical = ready && anomaly.statistical_anomaly;
  return <article className="ai-analysis-card" aria-labelledby="anomaly-heading">
    <div className="overview-heading"><h3 id="anomaly-heading">Détection d’anomalies</h3>
      <StatusBadge state={ready ? atypical ? 'warning' : 'operational' : 'unknown'}
        label={ready ? atypical ? 'Observation atypique' : 'Observation habituelle' : 'Analyse indisponible'} /></div>
    {ready ? <>
      <p className="ops-caption">Observations du {date(data.input_evaluated_at)} · Isolation Forest.</p>
      <dl className="ai-score-grid"><div><dt>Score d’anomalie</dt><dd>{scores.format(anomaly.score)}</dd></div>
        <div><dt>Seuil de détection</dt><dd>{scores.format(anomaly.threshold)}</dd></div></dl>
      <p className="ops-caption">{atypical ? 'Les ressources présentent un comportement inhabituel par rapport au modèle.'
        : 'Le score reste dans la zone habituelle du modèle.'}</p>
      <p className="ops-note">Le score décrit le comportement des ressources ; il ne mesure pas une probabilité de panne.</p>
      <DetailsPanel title="Modèle et observations"><ModelFacts data={data} name="Isolation Forest" /></DetailsPanel>
    </> : <Unavailable result={result} data={data} />}
  </article>;
}

export function Forecasts({ result = {}, now }) {
  const data = result.data;
  const ready = !result.pending && !result.error && !result.stale && analysisReady(data, now);
  return <article className="forecast-card ai-analysis-card" aria-labelledby="forecast-heading">
    <div className="overview-heading"><h3 id="forecast-heading">Prévisions des ressources</h3>
      <StatusBadge state={ready ? 'operational' : 'unknown'} label={ready ? 'Prévisions disponibles' : 'Analyse indisponible'} /></div>
    {ready ? <>
      <p className="ops-caption">Estimations fondées sur les observations du {date(data.input_evaluated_at)}.</p>
      <CompactTable caption="Prévisions des ressources à 5, 10 et 15 minutes" rows={RESOURCES} rowKey={([key]) => key}
        columns={[{ key: 'resource', label: 'Ressource', render: ([, title]) => title },
          ...['5', '10', '15'].map((h) => ({ key: h, label: '+' + h + ' min',
            render: ([key]) => <strong>{numbers.format(data.forecasts[h].metrics[key].value)} %</strong> }))]} />
      <DetailsPanel title="Modèle, méthodes et horizons de prévision">
        <ModelFacts data={data} name="Régression Ridge avec référence de persistance" />
        {['5', '10', '15'].map((horizon) => <div className="ai-horizon" key={horizon}>
          <h4>À {horizon} minutes · {date(data.forecasts[horizon].target_evaluated_at)}</h4>
          <SummaryList rows={RESOURCES.map(([key, title]) => [title, methods[data.forecasts[horizon].metrics[key].method]])} />
        </div>)}
      </DetailsPanel>
    </> : <Unavailable result={result} data={data} />}
  </article>;
}

export function AnalysisView({ anomaly, forecast, now }) {
  return <div className="ai-analysis-view"><Anomalies result={anomaly} now={now} /><Forecasts result={forecast} now={now} /></div>;
}
