export const NAVIGATION = [
  { id: 'main', title: 'Tableau de bord', description: 'Ressources actuelles de l’hôte Moodle.', icon: 'grid', color: '#8f5fe8' },
  { id: 'applications', title: 'Services Moodle', description: 'Disponibilité de Moodle, de PHP-FPM et de PostgreSQL.', icon: 'server', color: '#0090e7' },
  { id: 'containers', title: 'Conteneurs', description: 'État et consommation des conteneurs Moodle essentiels.', icon: 'cpu', color: '#8f5fe8' },
  { id: 'analysis', title: 'Prévisions', description: 'Estimation des ressources à 5, 10 et 15 minutes.', icon: 'chart', color: '#00d25b' },
  { id: 'incidents', title: 'Incidents et décisions', description: 'Épisodes détectés et recommandations de correction.', icon: 'alert', color: '#ffab00' },
  { id: 'audits', title: 'Journaux d’exécution', description: 'Historique des diagnostics et des remédiations.', icon: 'clock', color: '#0090e7' },
  { id: 'backups', title: 'Sauvegardes', description: 'Suivi des sauvegardes et des points de reprise.', icon: 'disk', color: '#8f5fe8' },
  { id: 'policies', title: 'Politiques', description: 'Règles qui encadrent les corrections.', icon: 'server', color: '#0090e7' },
  { id: 'php-profiles', title: 'Profils PHP-FPM', description: 'Configuration des profils de capacité PHP-FPM.', icon: 'server', color: '#00d25b' },
  { id: 'reports', title: 'Rapports', description: 'Consultation et export des opérations enregistrées.', icon: 'chart', color: '#0090e7' },
];

export function sectionFromHash(hash, accountsEnabled = false) {
  const id = typeof hash === 'string' ? hash.replace(/^#/, '') : '';
  return NAVIGATION.some((item) => item.id === id) || (id === 'accounts' && accountsEnabled) ? id : 'main';
}
