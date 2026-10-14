# Plateforme de pilotage et de résilience Moodle

Ce dépôt contient le pilote du PFE : Moodle conteneurisé, supervision Prometheus, détection d’anomalies Isolation Forest, prévisions à 5/10/15 minutes, incidents, diagnostics guidés et interventions encadrées depuis un tableau de bord React.

Le parcours de démonstration est prévu pour **GitHub Codespaces** sur la branche `demo-codespaces`. Il ne nécessite pas de VM dédiée.

## Démarrage dans Codespaces

Le conteneur de développement prépare Python 3.12, Ansible, Node et Docker-in-Docker sans lancer le déploiement automatiquement. Dans le terminal du Codespace :

```bash
bash scripts/codespaces/deploy.sh
sudo moodle-pilotage-user add --username admin --name "Administrateur PFE" --role admin
bash scripts/codespaces/status.sh
```

Le script affiche ensuite les URLs privées de Moodle et du tableau de bord. La procédure complète, les artefacts IA et les limites de démonstration sont détaillés dans [docs/codespaces-pilot.md](docs/codespaces-pilot.md).

## Garde-fous du pilote

- Les politiques sont initialisées en mode `observation`.
- Les diagnostics déclenchés depuis React sont exécutés en lecture seule par un worker distinct.
- Le worker de remédiation automatique n’est pas installé dans le parcours Codespaces.
- Le playbook Codespaces n’importe aucun rôle Restic, PRA, restauration ou campagne de panne.
- La base PostgreSQL du pilotage n’est pas sauvegardée par ce parcours.

Le détail du nouveau flux de diagnostic est disponible dans [docs/diagnostics-guides.md](docs/diagnostics-guides.md).
