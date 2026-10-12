import { useEffect, useState } from 'react';
import Operations from './Operations.jsx';
import { useMetrics } from './useMetrics.js';
import { useAuth } from './Auth.jsx';
import Accounts from './Accounts.jsx';
import DecisionHistory from './DecisionHistory.jsx';
import Backups from './Backups.jsx';
import ApplicationServices, { DetailsPanel, Overview, ResourceMetrics } from './ApplicationServices.jsx';
import Containers from './Containers.jsx';
import Reports from './Reports.jsx';
import Policies from './Policies.jsx';
import PHPProfiles from './PHPProfiles.jsx';
import { initials, canManageUsers } from './authClient.js';
import { displayDate, NAVIGATION, NAVIGATION_GROUPS, sectionFromHash } from './navigation.js';

// Sidebar structure adapted from BootstrapDash Corona React (MIT).
const dateFormat = new Intl.DateTimeFormat('fr-FR', { day: 'numeric', month: 'long', year: 'numeric' });
const menuTitles = { 'decision-history': 'Décisions', audits: 'Interventions', policies: 'Mode de pilotage' };

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

export function SectionContent({ section, now, refreshToken, metrics, view, incidentFocus, onNavigate }) {
  const snapshot = view || { metrics, collection: {}, reason: 'pending' };
  switch (section) {
    case 'main': return <Overview view={snapshot} now={now} refreshToken={refreshToken} onNavigate={onNavigate} />;
    case 'resources': return <section id="resources" aria-label="Ressources de l’hôte Moodle">
      <ResourceMetrics metrics={snapshot.metrics} />
      <DetailsPanel title="Détails de la collecte"><dl className="summary-facts">
        <div><dt>Collecte des ressources</dt><dd>{snapshot.collection.linux?.status === 'up' ? 'Récente' : 'Indisponible ou ancienne'}</dd></div>
        <div><dt>Âge des mesures</dt><dd>{Number.isFinite(snapshot.collection.linux?.age) ? Math.round(snapshot.collection.linux.age) + ' s' : 'Inconnu'}</dd></div>
        <div><dt>Lecture du dashboard</dt><dd>{displayDate(now / 1000)}</dd></div>
      </dl></DetailsPanel>
    </section>;
    case 'applications': return <ApplicationServices now={now} view={snapshot} refreshToken={refreshToken} />;
    case 'containers': return <Containers now={now} refreshToken={refreshToken} />;
    case 'analysis': case 'incidents': case 'audits':
      return <Operations section={section} now={now} refreshToken={refreshToken} incidentFocus={incidentFocus} onNavigate={onNavigate} />;
    case 'backups': return <Backups now={now} refreshToken={refreshToken} />;
    case 'policies': return <Policies refreshToken={refreshToken} />;
    case 'php-profiles': return <PHPProfiles now={now} refreshToken={refreshToken} />;
    case 'reports': return <Reports refreshToken={refreshToken} />;
    case 'decision-history': return <DecisionHistory refreshToken={refreshToken} now={now} />;
    case 'accounts': return <Accounts />;
    default: return null;
  }
}

export function SidebarNavigation({ items, activeSection, navigate }) {
  const [expanded, setExpanded] = useState(() => NAVIGATION_GROUPS.filter((group) => group.sections.includes(activeSection)).map((group) => group.id));
  useEffect(() => {
    const group = NAVIGATION_GROUPS.find((group) => group.sections.includes(activeSection));
    if (group) setExpanded((before) => before.includes(group.id) ? before : [...before, group.id]);
  }, [activeSection]);
  function link(item) {
    return <li key={item.id} className={`nav-item menu-items ${activeSection === item.id ? 'active' : ''}`}>
      <a className="nav-link" href={`#${item.id}`} aria-current={activeSection === item.id ? 'page' : undefined}
        onClick={(event) => { event.preventDefault(); navigate(item.id); }}>
        <span className="menu-icon"><Icon name={item.icon} size={18} /></span><span className="menu-title">{menuTitles[item.id] || item.title}</span>
      </a></li>;
  }
  function group(group) {
    const members = group.sections.map((id) => items.find((item) => item.id === id)).filter(Boolean);
    return <li key={group.id} className="nav-group">
      <button className="nav-group-toggle" aria-expanded={expanded.includes(group.id)} aria-controls={'nav-' + group.id}
        onClick={() => setExpanded((before) => before.includes(group.id) ? before.filter((id) => id !== group.id) : [...before, group.id])}>
        <Icon name={group.icon} size={18} /><span>{group.title}</span><span className="nav-chevron" aria-hidden="true">⌄</span>
      </button>
      <ul id={'nav-' + group.id} className="nav-submenu" hidden={!expanded.includes(group.id)}>{members.map(link)}</ul>
    </li>;
  }
  return <nav aria-label="Navigation principale"><ul className="nav">
    {link(items.find((item) => item.id === 'main'))}
    {NAVIGATION_GROUPS.slice(0, 2).map(group)}
    {link(items.find((item) => item.id === 'backups'))}
    {group(NAVIGATION_GROUPS[2])}
  </ul></nav>;
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
  const [incidentFocus, setIncidentFocus] = useState(null);
  const metricsEnabled = ['main', 'resources', 'applications'].includes(activeSection);
  const { error, busy, now, view, refresh } = useMetrics(metricsEnabled);
  const current = items.find((item) => item.id === activeSection) || items[0];
  useEffect(() => {
    const followLocation = () => {
      setActiveSection(sectionFromHash(window.location.hash, auth.enabled)); setIncidentFocus(null); setMenuOpen(false);
    };
    followLocation();
    window.addEventListener('hashchange', followLocation);
    window.addEventListener('popstate', followLocation);
    return () => {
      window.removeEventListener('hashchange', followLocation);
      window.removeEventListener('popstate', followLocation);
    };
  }, [auth.enabled]);
  useEffect(() => {
    const closeOnEscape = (event) => {
      if (event.key === 'Escape' && menuOpen) { setMenuOpen(false); document.querySelector('.menu-toggle')?.focus(); }
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [menuOpen]);
  const navigate = (id, focusedIncident = null) => {
    const section = sectionFromHash('#' + id, auth.enabled);
    if (window.location.hash !== '#' + section) window.history.pushState(null, '', '#' + section);
    setIncidentFocus(focusedIncident); setActiveSection(section); setMenuOpen(false); window.scrollTo(0, 0);
    window.requestAnimationFrame(() => document.getElementById('content')?.focus({ preventScroll: true }));
  };
  const metrics = view.metrics;
  const notice = !metricsEnabled ? null : view.reason === 'error' ? error : view.reason === 'stale'
    ? 'Les données sont anciennes. Actualisez pour obtenir des mesures récentes.'
    : view.reason === 'clock' ? 'L’heure des mesures est incohérente. Les valeurs sont indisponibles.'
      : view.reason === 'ready' && view.collection.linux.status !== 'up'
        ? 'La collecte des ressources est indisponible. Les mesures concernées sont masquées.' : null;
  const update = () => { if (metricsEnabled) refresh(); setOperationsRefresh((value) => value + 1); };
  const refreshing = metricsEnabled && busy;

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
      <SidebarNavigation items={items} activeSection={activeSection} navigate={navigate} />
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
        <div className="page-heading"><div><h1>{menuTitles[current.id] || current.title}</h1><p>{current.description}</p></div>
          <button className="refresh-button" type="button" aria-label={`Actualiser ${current.title}`} onClick={update} disabled={refreshing}>
            <Icon name="refresh" size={17} className={refreshing ? 'spin' : ''} /><span>{refreshing ? 'Actualisation…' : 'Actualiser'}</span>
          </button></div>
        {(notice || auth.message) && <div className="notice" role="status"><Icon name="alert" size={18} /><span>{auth.message || notice}</span></div>}
        <SectionContent section={activeSection} now={now} refreshToken={operationsRefresh} metrics={metrics} view={view}
          incidentFocus={incidentFocus} onNavigate={navigate} />
        <footer className="page-footer"><span>BeeCoders · Pilotage Moodle</span></footer>
      </main>
    </div>
  </div>;
}
