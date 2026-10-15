# Plateforme de pilotage et de résilience Moodle

Ce dépôt contient le pilote du PFE : Moodle conteneurisé, supervision Prometheus, détection d’anomalies Isolation Forest, incidents, diagnostics guidés, interventions encadrées et récupération pédagogique depuis un tableau de bord React.

Le parcours de démonstration est prévu pour **GitHub Codespaces** sur la branche `demo-codespaces`. Il ne nécessite pas de VM dédiée.

## Démarrage dans Codespaces

Le conteneur de développement prépare Python 3.12, Ansible, Node et Docker-in-Docker sans lancer le déploiement automatiquement. Dans le terminal du Codespace :

```bash
bash scripts/codespaces/deploy.sh
sudo moodle-pilotage-user add --username admin --name "Administrateur PFE" --role admin
bash scripts/codespaces/status.sh
```

Le script affiche ensuite les URLs privées de Moodle et du tableau de bord. La procédure complète, les artefacts IA et les limites de démonstration sont détaillés dans [docs/codespaces-pilot.md](docs/codespaces-pilot.md).

La sauvegarde et la restauration fonctionnelles d'un cours depuis le dashboard
sont décrites dans [docs/sauvegarde-restauration-cours.md](docs/sauvegarde-restauration-cours.md).

## Garde-fous du pilote

- Les politiques sont initialisées en mode `observation`.
- Les diagnostics déclenchés depuis React sont exécutés en lecture seule par un worker distinct.
- La remédiation reste limitée aux actions explicitement autorisées.
- La restauration de cours crée toujours une copie ; le PRA complet reste exclu.
- La base PostgreSQL du pilotage n’est pas sauvegardée par ce parcours.

Le détail du nouveau flux de diagnostic est disponible dans [docs/diagnostics-guides.md](docs/diagnostics-guides.md).
