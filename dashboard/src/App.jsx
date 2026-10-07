import { useState } from 'react';
import Operations from './Operations.jsx';
import { chartSeries } from './snapshot.js';
import { useMetrics } from './useMetrics.js';

const numberFormat = new Intl.NumberFormat('fr-FR', {
  maximumFractionDigits: 1,
});
const timeFormat = new Intl.DateTimeFormat('fr-FR', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
});
const shortTimeFormat = new Intl.DateTimeFormat('fr-FR', {
  hour: '2-digit',
  minute: '2-digit',
});
const dateFormat = new Intl.DateTimeFormat('fr-FR', {
  day: 'numeric',
  month: 'long',
  year: 'numeric',
});
const formatNumber = (value) =>
  value === null ? '—' : numberFormat.format(value);
const sourceLabels = {
  up: 'Collecte active',
  down: 'Collecte en échec',
  stale: 'Données anciennes',
  missing: 'Données absentes',
  unknown: 'État inconnu',
};

function Icon({ name, size = 20, ...props }) {
  const paths = {
    server: (
      <>
        <rect x="3" y="3" width="18" height="7" rx="2" />
        <rect x="3" y="14" width="18" height="7" rx="2" />
        <path d="M7 6.5h.01M7 17.5h.01M11 6.5h6M11 17.5h6" />
      </>
    ),
    grid: (
      <>
        <rect x="3" y="3" width="7" height="7" rx="1.5" />
        <rect x="14" y="3" width="7" height="7" rx="1.5" />
        <rect x="3" y="14" width="7" height="7" rx="1.5" />
        <rect x="14" y="14" width="7" height="7" rx="1.5" />
      </>
    ),
    chart: (
      <>
        <path d="M4 3v17h17M8 14l4-5 4 3 4-7" />
      </>
    ),
    cpu: (
      <>
        <rect x="6" y="6" width="12" height="12" rx="2" />
        <rect x="9" y="9" width="6" height="6" rx="1" />
        <path d="M9 3v3m6-3v3M9 18v3m6-3v3M3 9h3m-3 6h3m12-6h3m-3 6h3" />
      </>
    ),
    memory: (
      <>
        <rect x="3" y="6" width="18" height="11" rx="2" />
        <path d="M7 10v3m5-3v3m5-3v3M7 17v3m5-3v3m5-3v3" />
      </>
    ),
    disk: (
      <>
        <path d="M6 3h12l3 12H3L6 3Z" />
        <rect x="3" y="15" width="18" height="6" rx="2" />
        <path d="M16 18h.01M12 18h.01" />
      </>
    ),
    pulse: <path d="M2 12h5l3-8 4 16 3-8h5" />,
    clock: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7v5l3 2" />
      </>
    ),
    refresh: (
      <>
        <path d="M20 7v5h-5M4 17v-5h5" />
        <path d="M6 7a7 7 0 0 1 12-2l2 2M4 17l2 2a7 7 0 0 0 12-2" />
      </>
    ),
    arrow: (
      <>
        <path d="M8 5h11v11M19 5 5 19" />
      </>
    ),
    check: <path d="m5 12 4 4L19 6" />,
    alert: (
      <>
        <path d="M12 3 2 20h20L12 3Z" />
        <path d="M12 9v4m0 3h.01" />
      </>
    ),
    globe: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M3 12h18M12 3c6 6 6 12 0 18-6-6-6-12 0-18Z" />
      </>
    ),
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {paths[name] || paths.server}
    </svg>
  );
}

function SourcePill({ status }) {
  return (
    <span className={`pill source-${status}`}>
      <span className="status-dot" />
      {sourceLabels[status]}
    </span>
  );
}

function MetricCard({
  label,
  value,
  unit,
  icon,
  hint,
  color,
  percentage = false,
}) {
  return (
    <article className="metric-card" data-metric={label}>
      <div className="metric-top">
        <span>{label}</span>
        <span className={`metric-icon ${color}`}>
          <Icon name={icon} />
        </span>
      </div>
      <div className={`metric-value ${value === null ? 'missing' : ''}`}>
        {formatNumber(value)}
        {value !== null && <span className="unit">{unit}</span>}
      </div>
      {percentage && (
        <div className="meter" aria-hidden="true">
          <div className={color} style={{ width: `${value ?? 0}%` }} />
        </div>
      )}
      <p className="metric-hint">
        {value === null ? 'Mesure indisponible' : hint}
      </p>
    </article>
  );
}

function ResourceChart({ samples }) {
  const series = [
    { key: 'cpu_usage_percent', label: 'CPU', color: '#3975e7' },
    { key: 'memory_usage_percent', label: 'Mémoire', color: '#1c9d93' },
    {
      key: 'disk_available_percent',
      label: 'Disque disponible',
      color: '#9671d4',
    },
  ];
  const validSamples = samples.filter((sample) =>
    series.some((item) => sample[item.key] !== null),
  ).length;
  return (
    <section className="panel history-panel" aria-labelledby="history-heading">
      <div className="panel-heading">
        <div>
          <h2 id="history-heading">Évolution des ressources</h2>
          <p>Mesures collectées depuis l’ouverture de cette page.</p>
        </div>
        <span className="subtle-badge">
          {validSamples} relevé{validSamples !== 1 ? 's' : ''}
        </span>
      </div>
      <div className="chart-legend">
        {series.map((item) => (
          <span key={item.key}>
            <i style={{ background: item.color }} />
            {item.label}
          </span>
        ))}
      </div>
      {samples.length ? (
        <div className="chart-container">
          <div className="chart-frame">
            <div className="chart-y-axis" aria-hidden="true">
              {[100, 75, 50, 25, 0].map((value) => (
                <span key={value}>{value}</span>
              ))}
            </div>
            <svg
              className="resource-chart"
              viewBox="0 0 680 164"
              preserveAspectRatio="none"
              role="img"
              aria-label="Évolution du CPU, de la mémoire utilisée et de l’espace disque disponible, en pourcentage."
            >
              {[100, 75, 50, 25, 0].map((value) => {
                const y = 164 - (value / 100) * 164;
                return (
                  <line
                    key={value}
                    x1="0"
                    x2="680"
                    y1={y}
                    y2={y}
                    vectorEffect="non-scaling-stroke"
                  />
                );
              })}
              {series.map((item) => {
                const { path, lastPoint } = chartSeries(samples, item.key);
                return (
                  <g key={item.key}>
                    <path
                      d={path}
                      stroke={item.color}
                      fill="none"
                      strokeWidth="2.3"
                      strokeLinejoin="round"
                      strokeLinecap="round"
                      vectorEffect="non-scaling-stroke"
                    />
                    {lastPoint && (
                      <path
                        d={`M${lastPoint.x},${lastPoint.y}l0,0`}
                        stroke={item.color}
                        strokeWidth="6"
                        strokeLinecap="round"
                        vectorEffect="non-scaling-stroke"
                      />
                    )}
                  </g>
                );
              })}
            </svg>
          </div>
          <div className="chart-x-axis" aria-hidden="true">
            <span>{shortTimeFormat.format(samples[0].time)}</span>
            <span>{shortTimeFormat.format(samples.at(-1).time)}</span>
          </div>
        </div>
      ) : (
        <div className="chart-empty">
          <Icon name="chart" size={30} />
          <p>Les premières mesures apparaîtront ici.</p>
        </div>
      )}
      <div className="chart-footnote">
        <Icon name="clock" size={14} />
        {samples.length < 2
          ? 'En attente des prochaines mesures'
          : 'Jusqu’à 60 mesures · les interruptions restent visibles'}
      </div>
    </section>
  );
}

function SourceCard({ title, description, source, icon }) {
  return (
    <article className="source-card">
      <div className="source-symbol">
        <Icon name={icon} size={22} />
      </div>
      <div className="source-details">
        <h3>{title}</h3>
        <p>{description}</p>
      </div>
      <div className="source-state">
        <SourcePill status={source.status} />
        <span>
          {source.status === 'unknown' || source.age === null
            ? 'Pas de mesure utilisable'
            : `Âge estimé : ${formatNumber(Math.max(0, source.age))} s`}
        </span>
      </div>
    </article>
  );
}

export default function App() {
  const [operationsRefresh, setOperationsRefresh] = useState(0);
  const { snapshot, error, busy, samples, now, view, refresh } = useMetrics();
  const metrics = view.metrics;
  const availability = metrics.moodle_available;
  const moodleState =
    availability === true
      ? 'available'
      : availability === false
        ? 'unavailable'
        : 'unknown';
  const title =
    availability === true
      ? 'Moodle est disponible'
      : availability === false
        ? 'La sonde Moodle a échoué'
        : 'État de Moodle inconnu';
  const description =
    availability === true
      ? 'La page de connexion répond correctement en HTTPS.'
      : availability === false
        ? 'La dernière vérification HTTPS n’a pas abouti avec succès.'
        : 'Une mesure récente est nécessaire pour confirmer sa disponibilité.';
  const collectionProblem = Object.values(view.collection).some(
    (source) => source.status !== 'up',
  );
  const notice =
    view.reason === 'error'
      ? error
      : view.reason === 'stale'
        ? 'La dernière lecture date de plus de 60 secondes. Les valeurs actuelles sont masquées.'
        : view.reason === 'clock'
          ? 'L’horodatage reçu est incohérent avec l’heure actuelle. Les valeurs actuelles sont masquées.'
          : view.reason === 'ready' && collectionProblem
            ? 'Une collecte est absente, en échec ou trop ancienne. Les mesures concernées sont indisponibles.'
            : null;
  const httpCode = metrics.moodle_http_status_code;

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main">
        Aller au contenu
      </a>
      <aside className="sidebar">
        <a className="brand" href="#main">
          <span className="brand-mark">
            <Icon name="server" size={22} />
          </span>
          <span>
            BeeCoders<small>Pilotage Moodle</small>
          </span>
        </a>
        <div className="sidebar-label">SUPERVISION</div>
        <nav aria-label="Navigation principale">
          <a className="nav-item active" href="#main">
            <Icon name="grid" />
            Vue d’ensemble
          </a>
          <a className="nav-item" href="#resources">
            <Icon name="chart" />
            Ressources serveur
          </a>
          <a className="nav-item" href="#collection">
            <Icon name="pulse" />
            Qualité de la collecte
          </a>
          <a className="nav-item" href="#analysis"><Icon name="chart" />Intelligence artificielle</a>
          <a className="nav-item" href="#incidents"><Icon name="alert" />Incidents et décisions</a>
          <a className="nav-item" href="#audits"><Icon name="clock" />Journaux d’exécution</a>
        </nav>
        <div className="sidebar-environment">
          <span className="sidebar-label">ENVIRONNEMENT</span>
          <strong>
            <span className="environment-dot" />
            Pilote Moodle
          </strong>
          <p>Infrastructure de laboratoire</p>
        </div>
        <div className="sidebar-bottom">
          <span className="sidebar-avatar">MK</span>
          <div>
            <strong>Administration</strong>
            <small>Version 0.2.0</small>
          </div>
        </div>
      </aside>

      <div className="workspace">
        <header className="topbar">
          <span className="breadcrumb">
            BeeCoders <span>/</span> Pilote Moodle
          </span>
          <span className="today">{dateFormat.format(now)}</span>
        </header>
        <main id="main">
          <div className="page-heading">
            <div>
              <span className="eyebrow">SUPERVISION DU PILOTE</span>
              <h1>Vue d’ensemble</h1>
              <p>L’état de Moodle et de ses ressources, au même endroit.</p>
            </div>
            <button
              className="refresh-button"
              type="button"
              onClick={() => { refresh(); setOperationsRefresh((value) => value + 1); }}
              disabled={busy}
            >
              <Icon name="refresh" className={busy ? 'spin' : ''} />
              {busy ? 'Actualisation…' : 'Actualiser'}
            </button>
          </div>

          <div className="update-line">
            <span
              className={`update-dot ${view.reason === 'ready' ? 'live' : ''}`}
            />
            <span>
              {view.reason === 'pending'
                ? 'Première lecture en cours'
                : 'Actualisation automatique · 15 s'}
            </span>
            <span className="update-separator">·</span>
            <span>
              Dernière lecture réussie :{' '}
              <strong>
                {snapshot
                  ? timeFormat.format(Date.parse(snapshot.evaluated_at))
                  : '—'}
              </strong>
            </span>
          </div>

          {notice && (
            <div className="notice" role="status">
              <Icon name="alert" size={19} />
              <span>{notice}</span>
            </div>
          )}

          <section
            className={`availability-panel ${moodleState}`}
            aria-labelledby="availability-heading"
          >
            <span className="availability-symbol">
              <Icon
                name={
                  availability === true
                    ? 'check'
                    : availability === false
                      ? 'alert'
                      : 'globe'
                }
                size={27}
              />
            </span>
            <div className="availability-copy">
              <div className="availability-label">SONDE HTTPS MOODLE</div>
              <h2 id="availability-heading">{title}</h2>
              <p>{description}</p>
            </div>
            <div className="availability-right">
              <span className={`http-badge ${moodleState}`}>
                {httpCode === null
                  ? 'HTTP non mesuré'
                  : httpCode === 0
                    ? 'Sans réponse HTTP'
                    : `HTTP ${httpCode}`}
              </span>
              <a
                href="https://moodle-pfe.test:8443/login/index.php"
                target="_blank"
                rel="noreferrer"
              >
                Ouvrir Moodle <Icon name="arrow" size={15} />
              </a>
            </div>
          </section>

          <section
            id="resources"
            className="metrics-grid"
            aria-label="Mesures actuelles"
          >
            <MetricCard
              label="CPU utilisé"
              value={metrics.cpu_usage_percent}
              unit="%"
              icon="cpu"
              color="blue"
              percentage
              hint="Moyenne sur les 2 dernières minutes"
            />
            <MetricCard
              label="Mémoire utilisée"
              value={metrics.memory_usage_percent}
              unit="%"
              icon="memory"
              color="teal"
              percentage
              hint="Part de la mémoire utilisée"
            />
            <MetricCard
              label="Disque disponible"
              value={metrics.disk_available_percent}
              unit="%"
              icon="disk"
              color="purple"
              percentage
              hint="Espace disponible sur le disque racine"
            />
            <MetricCard
              label="Durée de la sonde"
              value={metrics.moodle_probe_duration_ms}
              unit="ms"
              icon="clock"
              color="amber"
              hint="Page de connexion en HTTPS"
            />
          </section>

          <ResourceChart samples={samples} />

          <section
            id="collection"
            className="panel collection-panel"
            aria-labelledby="collection-heading"
          >
            <div className="panel-heading">
              <div>
                <h2 id="collection-heading">Qualité de la collecte</h2>
                <p>
                  Une source doit être récente pour fournir des mesures
                  utilisables.
                </p>
              </div>
              <span className="subtle-badge">2 sources</span>
            </div>
            <div className="sources-grid">
              <SourceCard
                title="Serveur Linux"
                description="CPU, mémoire et disque"
                icon="server"
                source={view.collection.linux}
              />
              <SourceCard
                title="Sonde HTTPS Moodle"
                description="Disponibilité et durée de la sonde"
                icon="globe"
                source={view.collection.moodle_https}
              />
            </div>
          </section>

          <Operations now={now} refreshToken={operationsRefresh} />

          <footer className="page-footer">
            <span>Pilote Moodle · BeeCoders</span>
            <a href="/api/v1/metrics/summary" target="_blank" rel="noreferrer">
              Voir les mesures JSON <Icon name="arrow" size={13} />
            </a>
          </footer>
        </main>
      </div>
    </div>
  );
}
