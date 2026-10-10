import { useState } from 'react';
import Operations from './Operations.jsx';
import { useMetrics } from './useMetrics.js';
import { useAuth } from './Auth.jsx';
import Accounts from './Accounts.jsx';
import Backups from './Backups.jsx';
import ApplicationServices from './ApplicationServices.jsx';
import Reports from './Reports.jsx';
import Policies from './Policies.jsx';
import PHPProfiles from './PHPProfiles.jsx';
import { initials, canManageUsers } from './authClient.js';

// Sidebar structure adapted from BootstrapDash Corona React (MIT).
const numberFormat = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 1 });
const dateFormat = new Intl.DateTimeFormat('fr-FR', { day: 'numeric', month: 'long', year: 'numeric' });

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

function MetricCard({ label, value, icon, hint, color }) {
  return <article className="card metric-card" data-metric={label}>
    <div className="metric-top"><span className="metric-icon" style={{ '--accent': color }}><Icon name={icon} size={23} /></span>
      <span className="metric-label">{label}</span></div>
    <div className={`metric-value ${value === null ? 'missing' : ''}`}>
      {value === null ? '—' : numberFormat.format(value)}{value !== null && <span className="unit">%</span>}
    </div>
    <div className="meter" aria-hidden="true"><div style={{ width: `${value ?? 0}%`, background: color }} /></div>
    <p className="metric-hint">{value === null ? 'Mesure indisponible' : hint}</p>
  </article>;
}

const navigation = [
  { id: 'main', title: 'Tableau de bord', icon: 'grid', color: '#8f5fe8' },
  { id: 'applications', title: 'Services Moodle', icon: 'server', color: '#0090e7' },
  { id: 'analysis', title: 'Prévisions', icon: 'chart', color: '#00d25b' },
  { id: 'incidents', title: 'Incidents et décisions', icon: 'alert', color: '#ffab00' },
  { id: 'audits', title: 'Journaux d’exécution', icon: 'clock', color: '#0090e7' },
  { id: 'backups', title: 'Sauvegardes', icon: 'disk', color: '#8f5fe8' },
  { id: 'policies', title: 'Politiques', icon: 'server', color: '#0090e7' },
  { id: 'php-profiles', title: 'Profils PHP-FPM', icon: 'server', color: '#00d25b' },
  { id: 'reports', title: 'Rapports', icon: 'chart', color: '#0090e7' },
];

export default function App() {
  const auth = useAuth();
  const avatar = initials(auth.user);
  const items = auth.enabled ? [...navigation, { id: 'accounts',
    title: canManageUsers(auth.user) ? 'Comptes et accès' : 'Mon compte', icon: 'server', color: '#0090e7' }] : navigation;
  const [operationsRefresh, setOperationsRefresh] = useState(0);
  const [activeSection, setActiveSection] = useState('main');
  const [menuOpen, setMenuOpen] = useState(false);
  const { error, busy, now, view, refresh } = useMetrics();
  const metrics = view.metrics;
  const notice = view.reason === 'error' ? error : view.reason === 'stale'
    ? 'Les données sont anciennes. Actualisez pour obtenir des mesures récentes.'
    : view.reason === 'clock' ? 'L’heure des mesures est incohérente. Les valeurs sont indisponibles.'
      : view.reason === 'ready' && view.collection.linux.status !== 'up'
        ? 'La collecte des ressources est indisponible. Les mesures concernées sont masquées.' : null;
  const update = () => { refresh(); setOperationsRefresh((value) => value + 1); };

  return <div className={`container-scroller app-shell ${menuOpen ? 'menu-open' : ''}`}>
    <a className="skip-link" href="#main">Aller au contenu</a>
    <aside className="sidebar sidebar-offcanvas" id="sidebar" aria-label="Menu principal">
      <div className="sidebar-brand-wrapper"><a className="sidebar-brand" href="#main" onClick={() => setMenuOpen(false)}>
        <span className="brand-mark"><Icon name="server" size={26} /></span><span>Moodle<span className="brand-caption">BeeCoders</span></span>
      </a></div>
      <div className="profile-desc"><span className="profile-initials">{avatar}</span><div className="profile-name">
        <strong>{auth.user.display_name}</strong><span>{canManageUsers(auth.user) ? 'Administrateur' : 'Lecteur'}</span></div></div>
      <nav aria-label="Navigation principale"><ul className="nav">
        <li className="nav-category">Navigation</li>
        {items.map((item) => <li key={item.id} className={`nav-item menu-items ${activeSection === item.id ? 'active' : ''}`}>
          <a className="nav-link" href={`#${item.id}`} aria-current={activeSection === item.id ? 'location' : undefined}
            onClick={() => { setActiveSection(item.id); setMenuOpen(false); }}>
            <span className="menu-icon" style={{ color: item.color }}><Icon name={item.icon} size={18} /></span>
            <span className="menu-title">{item.title}</span></a></li>)}
      </ul></nav>
      <div className="sidebar-footer"><Icon name="server" size={16} /><span>Environnement pilote</span></div>
    </aside>
    {menuOpen && <button className="sidebar-backdrop" aria-label="Fermer le menu" onClick={() => setMenuOpen(false)} />}
    <div className="workspace page-body-wrapper">
      <header className="navbar topbar"><div className="navbar-start">
        <button className="menu-toggle" aria-label={menuOpen ? 'Fermer le menu' : 'Ouvrir le menu'}
          aria-expanded={menuOpen} aria-controls="sidebar" onClick={() => setMenuOpen((open) => !open)}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16" /></svg>
        </button><span className="navbar-brand-text">Pilotage Moodle</span></div>
        <div className="navbar-end"><span className="today">{dateFormat.format(now)}</span><span className="topbar-avatar">{avatar}</span>
          {auth.enabled && <div className="session-actions"><span className="session-role">{canManageUsers(auth.user) ? 'Administrateur' : 'Lecteur'}</span>
            <button className="refresh-button" onClick={auth.logout}>Déconnexion</button></div>}</div>
      </header>
      <main id="main" className="content-wrapper">
        <div className="page-heading"><div><h1>Tableau de bord</h1><p>Ressources, prévisions et suivi des incidents.</p></div>
          <button className="refresh-button" type="button" aria-label="Actualiser le tableau de bord" onClick={update} disabled={busy}>
            <Icon name="refresh" size={17} className={busy ? 'spin' : ''} /><span>{busy ? 'Actualisation…' : 'Actualiser'}</span>
          </button></div>
        {(notice || auth.message) && <div className="notice" role="status"><Icon name="alert" size={18} /><span>{auth.message || notice}</span></div>}
        <section id="resources" className="metrics-grid" aria-label="Mesures actuelles">
          <MetricCard label="CPU utilisé" value={metrics.cpu_usage_percent} icon="cpu" color="#0090e7" hint="Moyenne sur 2 minutes" />
          <MetricCard label="Mémoire utilisée" value={metrics.memory_usage_percent} icon="memory" color="#00d25b" hint="Part de la mémoire utilisée" />
          <MetricCard label="Disque disponible" value={metrics.disk_available_percent} icon="disk" color="#8f5fe8" hint="Espace disponible sur le disque racine" />
        </section>
        <ApplicationServices now={now} refreshToken={operationsRefresh} />
        <Operations now={now} refreshToken={operationsRefresh} />
        <Backups now={now} refreshToken={operationsRefresh} />
        <Policies refreshToken={operationsRefresh} />
        <PHPProfiles now={now} refreshToken={operationsRefresh} />
        <Reports refreshToken={operationsRefresh} />
        <Accounts />
        <footer className="page-footer"><span>BeeCoders · Pilotage Moodle</span><span>Interface Corona · v0.3.0</span></footer>
      </main>
    </div>
  </div>;
}
