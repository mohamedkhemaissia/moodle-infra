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
      <section className="ai-interpretation" aria-label="Interprétation de l’analyse">
        <h4>Comment interpréter cette observation ?</h4>
        <ul><li><strong>Anomalie statistique :</strong> le score dépasse le seuil du modèle.</li>
          <li><strong>Incident confirmé :</strong> les règles de supervision et l’historique des incidents doivent le confirmer.</li>
          <li><strong>Cause technique :</strong> elle est établie seulement après le diagnostic des services et des journaux.</li></ul>
        <p>Les valeurs CPU, mémoire et disque ci-dessus aident à orienter la vérification ; elles ne prouvent pas une cause.</p>
      </section>
      <DetailsPanel title="Afficher les détails du modèle" className="ai-technical-details">
        <dl className="summary-facts"><div><dt>Score d’anomalie</dt><dd>{scores.format(anomaly.score)}</dd></div>
          <div><dt>Seuil de détection</dt><dd>{scores.format(anomaly.threshold)}</dd></div>
          <div><dt>Algorithme</dt><dd>Isolation Forest</dd></div></dl>
        <p>Le score décrit un comportement inhabituel. Il ne représente pas une probabilité de panne.</p>
      </DetailsPanel>
    </> : <Unavailable result={result} data={data} />}
  </article>;
}

export function AnalysisView({ anomaly, now, onNavigate }) {
  return <div className="ai-analysis-view"><Anomalies result={anomaly} now={now} onNavigate={onNavigate} /></div>;
}
