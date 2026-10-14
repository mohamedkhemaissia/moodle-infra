# Pilote complet dans GitHub Codespaces

## Résultat attendu

Le Codespace héberge, dans un seul environnement de démonstration :

- Moodle et PostgreSQL applicatif sous Docker Compose ;
- Prometheus, Node Exporter, Blackbox Exporter, métriques PHP-FPM/PostgreSQL et métriques des conteneurs ;
- l’API FastAPI, le PostgreSQL séparé du pilotage et le tableau de bord React ;
- les incidents observés, les recommandations, l’historique et les exports CSV/HTML ;
- la file PostgreSQL des diagnostics guidés et son worker Ansible Runner en lecture seule ;
- les politiques persistantes initialisées en mode `observation`.

Le déploiement ne lance ni sauvegarde Restic, ni PRA/restauration, ni charge, ni panne volontaire, ni remédiation automatique.

## 1. Créer le Codespace

Créer le Codespace depuis la branche `demo-codespaces`, puis attendre la fin de `postCreateCommand`. La configuration se trouve dans `.devcontainer/devcontainer.json` et utilise Ubuntu 22.04, Python 3.12, Node 24 et Docker-in-Docker.

Le script d’installation est idempotent : il recalcule les empreintes des fichiers de dépendances et ne réinstalle que ce qui a changé. Il ne déploie aucun service.

## 2. Rendre les artefacts IA disponibles

Les données et modèles sont volontairement exclus de Git. Si les artefacts validés du pilote sont disponibles, les placer sous :

```text
ml/data/features/moodle_features_*/metadata.json
ml/data/models/moodle_anomalies_*/model.json
ml/data/models/moodle_anomalies_*/report.json
ml/data/models/moodle_forecasts_*/models.json
ml/data/models/moodle_forecasts_*/report.json
```

`deploy.sh` sélectionne la version locale complète la plus récente. Il est aussi possible de fournir explicitement des chemins absolus :

```bash
export MOODLE_FEATURES_DIR=/workspaces/moodle-infra/ml/data/features/moodle_features_VERSION
export MOODLE_ANOMALY_BUNDLE_DIR=/workspaces/moodle-infra/ml/data/models/moodle_anomalies_VERSION
export MOODLE_FORECAST_BUNDLE_DIR=/workspaces/moodle-infra/ml/data/models/moodle_forecasts_VERSION
```

En l’absence d’artefacts réels, le déploiement continue mais l’API affiche honnêtement l’IA comme indisponible. Aucun modèle fictif n’est créé. Lorsque les artefacts sont présents, les écrans exposent le score Isolation Forest et les horizons de prévision 5, 10 et 15 minutes.

## 3. Déployer le pilote

Depuis la racine du dépôt :

```bash
bash scripts/codespaces/deploy.sh
```

Après un `git pull`, si le script signale que les dépendances Codespaces ont
changé, les remettre à niveau puis relancer le déploiement :

```bash
bash scripts/codespaces/setup.sh
bash scripts/codespaces/deploy.sh
```

Ce script attend Docker, détecte les artefacts IA et exécute uniquement `ansible/playbooks/pilot-codespaces.yml`. Le playbook procède en trois étapes :

1. Moodle, supervision, journaux locaux, API et dashboard ;
2. migration des trois journaux du pilotage vers le PostgreSQL dédié ;
3. authentification, politiques en observation, diagnostics guidés, puis republication de l’API et du dashboard.

Il réutilise le Docker fourni par Codespaces et n’appelle jamais les rôles `linux`, `runtime`, `operations`, `recovery` ou les rôles de sauvegarde.
Le montage racine de `node-exporter` reste en lecture seule, sans propagation
`rslave`, car celle-ci n'est pas disponible avec Docker-in-Docker.

## 4. Créer l’unique administrateur

La création reste interactive afin qu’aucun mot de passe par défaut ne soit écrit dans Git ou dans les journaux :

```bash
sudo moodle-pilotage-user add \
  --username admin \
  --name "Administrateur PFE" \
  --role admin
```

Le mot de passe doit contenir au moins 15 caractères. Aucun second rôle n’est nécessaire pour la démonstration.

## 5. Vérifier sans provoquer de panne

```bash
bash scripts/codespaces/status.sh
```

Le contrôle lit seulement la santé de l’API, du dashboard, de Moodle, de la file PostgreSQL et des deux processus Supervisor. Il ne réserve pas de diagnostic et ne redémarre aucun service.

URLs privées du Codespace :

- Moodle : `https://${CODESPACE_NAME}-443.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}`
- tableau de bord : `https://${CODESPACE_NAME}-8088.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}`
- Prometheus : `https://${CODESPACE_NAME}-9090.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}`

Conserver les ports en visibilité **Private**.
Le domaine n’est jamais codé en dur : GitHub documente
[`GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN`](https://docs.github.com/en/codespaces/developing-in-a-codespace/default-environment-variables-for-your-codespace)
précisément pour construire ces URLs de façon portable.

## 6. Démonstration sûre

1. Se connecter au tableau de bord avec le compte administrateur.
2. Montrer les ressources, services applicatifs, conteneurs et état de collecte.
3. Montrer le score d’anomalie et les prévisions 5/10/15 minutes si les artefacts sont présents.
4. Ouvrir un incident réellement observé et expliquer les causes possibles, les mesures liées et l’avertissement de non-causalité.
5. Si une recommandation courante existe, cliquer sur **Demander un diagnostic**. La demande passe par React → FastAPI → PostgreSQL → worker → Ansible Runner et reste en lecture seule.
6. Montrer le résultat et son lien vers l’historique des diagnostics.
7. Montrer les politiques en mode `observation` et les exports CSV/HTML.

Ne pas créer artificiellement d’incident, ne pas arrêter de conteneur et ne pas passer la politique en mode `automatic` pendant cette phase.

## Dépannage borné

État des processus :

```bash
sudo supervisorctl status moodle-container-metrics moodle-diagnostic-requests
```

Journaux du worker de diagnostic :

```bash
sudo tail -n 100 /var/log/supervisor/moodle-diagnostic-requests.log
```

Validation locale du code, sans déploiement :

```bash
.venv/bin/python -B -m unittest \
  pilotage.tests.test_diagnostic_requests \
  diagnostics.tests.test_diagnostic_worker
npm --prefix dashboard test
npm --prefix dashboard run build
```

Les données de `/opt` et `/var/lib` appartiennent au conteneur Codespaces. Une reconstruction du devcontainer peut les supprimer. Dans le périmètre actuel, exporter les rapports utiles depuis l’interface ; ne pas activer une sauvegarde PostgreSQL du pilotage.
