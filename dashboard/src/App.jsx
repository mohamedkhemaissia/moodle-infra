import { useEffect, useState } from 'react';
import Operations from './Operations.jsx';
import { useMetrics } from './useMetrics.js';
import { useAuth } from './Auth.jsx';
import Accounts from './Accounts.jsx';
import Backups from './Backups.jsx';
import ApplicationServices from './ApplicationServices.jsx';
import Containers from './Containers.jsx';
import Reports from './Reports.jsx';
import Policies from './Policies.jsx';
import PHPProfiles from './PHPProfiles.jsx';
import { initials, canManageUsers } from './authClient.js';
import { NAVIGATION, sectionFromHash } from './navigation.js';

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

export function SectionContent({ section, now, refreshToken, metrics, onNavigate }) {
  switch (section) {
    case 'main': return <section id="resources" className="metrics-grid" aria-label="Mesures actuelles">
      <MetricCard label="CPU utilisé" value={metrics.cpu_usage_percent} icon="cpu" color="#0090e7" hint="Moyenne sur 2 minutes" />
      <MetricCard label="Mémoire utilisée" value={metrics.memory_usage_percent} icon="memory" color="#00d25b" hint="Part de la mémoire utilisée" />
      <MetricCard label="Disque disponible" value={metrics.disk_available_percent} icon="disk" color="#8f5fe8" hint="Espace disponible sur le disque racine" />
    </section>;
    case 'applications': return <ApplicationServices now={now} refreshToken={refreshToken} />;
    case 'containers': return <Containers now={now} refreshToken={refreshToken} />;
    case 'analysis': case 'incidents': case 'audits':
      return <Operations section={section} now={now} refreshToken={refreshToken} onNavigate={onNavigate} />;
    case 'backups': return <Backups now={now} refreshToken={refreshToken} />;
    case 'policies': return <Policies refreshToken={refreshToken} />;
    case 'php-profiles': return <PHPProfiles now={now} refreshToken={refreshToken} />;
    case 'reports': return <Reports refreshToken={refreshToken} />;
    case 'accounts': return <Accounts />;
    default: return null;
  }
}

export default function App() {
  const auth = useAuth();
  const avatar = initials(auth.user);
  const items = auth.enabled ? [...NAVIGATION, { id: 'accounts',
    title: canManageUsers(auth.user) ? 'Comptes et accès' : 'Mon compte',
    description: 'Gestion du compte et des accès au pilotage.', icon: 'server', color: '#0090e7' }] : NAVIGATION;
  const [operationsRefresh, setOperationsRefresh] = useState(0);
  const [activeSection, setActiveSection] = useState(() => sectionFromHash(globalThis.window?.location.hash, auth.enabled));
  const [menuOpen, setMenuOpen] = useState(false);
  const { error, busy, now, view, refresh } = useMetrics(activeSection === 'main');
  const current = items.find((item) => item.id === activeSection) || items[0];
  useEffect(() => {
    const followLocation = () => {
      setActiveSection(sectionFromHash(window.location.hash, auth.enabled)); setMenuOpen(false);
    };
    followLocation();
    window.addEventListener('hashchange', followLocation);
    window.addEventListener('popstate', followLocation);
    return () => {
      window.removeEventListener('hashchange', followLocation);
      window.removeEventListener('popstate', followLocation);
    };
  }, [auth.enabled]);
  const navigate = (id) => {
    const section = sectionFromHash('#' + id, auth.enabled);
    if (window.location.hash !== '#' + section) window.history.pushState(null, '', '#' + section);
    setActiveSection(section); setMenuOpen(false); window.scrollTo(0, 0);
  };
  const metrics = view.metrics;
  const notice = activeSection !== 'main' ? null : view.reason === 'error' ? error : view.reason === 'stale'
    ? 'Les données sont anciennes. Actualisez pour obtenir des mesures récentes.'
    : view.reason === 'clock' ? 'L’heure des mesures est incohérente. Les valeurs sont indisponibles.'
      : view.reason === 'ready' && view.collection.linux.status !== 'up'
        ? 'La collecte des ressources est indisponible. Les mesures concernées sont masquées.' : null;
  const update = () => { if (activeSection === 'main') refresh(); setOperationsRefresh((value) => value + 1); };
  const refreshing = activeSection === 'main' && busy;

  return <div className={`container-scroller app-shell ${menuOpen ? 'menu-open' : ''}`}>
    <a className="skip-link" href="#content" onClick={(event) => {
      event.preventDefault(); document.getElementById('content')?.focus();
    }}>Aller au contenu</a>
    <aside className="sidebar sidebar-offcanvas" id="sidebar" aria-label="Menu principal">
      <div className="sidebar-brand-wrapper"><a className="sidebar-brand" href="#main" onClick={(event) => {
        event.preventDefault(); navigate('main');
      }}>
        <span className="brand-mark"><Icon name="server" size={26} /></span><span>Moodle<span className="brand-caption">BeeCoders</span></span>
      </a></div>
      <div className="profile-desc"><span className="profile-initials">{avatar}</span><div className="profile-name">
        <strong>{auth.user.display_name}</strong><span>{canManageUsers(auth.user) ? 'Administrateur' : 'Lecteur'}</span></div></div>
      <nav aria-label="Navigation principale"><ul className="nav">
        <li className="nav-category">Navigation</li>
        {items.map((item) => <li key={item.id} className={`nav-item menu-items ${activeSection === item.id ? 'active' : ''}`}>
          <a className="nav-link" href={`#${item.id}`} aria-current={activeSection === item.id ? 'page' : undefined}
            onClick={(event) => { event.preventDefault(); navigate(item.id); }}>
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
      <main id="content" className="content-wrapper" tabIndex={-1}>
        <div className="page-heading"><div><h1>{current.title}</h1><p>{current.description}</p></div>
          <button className="refresh-button" type="button" aria-label={`Actualiser ${current.title}`} onClick={update} disabled={refreshing}>
            <Icon name="refresh" size={17} className={refreshing ? 'spin' : ''} /><span>{refreshing ? 'Actualisation…' : 'Actualiser'}</span>
          </button></div>
        {(notice || auth.message) && <div className="notice" role="status"><Icon name="alert" size={18} /><span>{auth.message || notice}</span></div>}
        <SectionContent section={activeSection} now={now} refreshToken={operationsRefresh} metrics={metrics} onNavigate={navigate} />
        <footer className="page-footer"><span>BeeCoders · Pilotage Moodle</span><span>Interface Corona · v0.3.0</span></footer>
      </main>
    </div>
  </div>;
}
