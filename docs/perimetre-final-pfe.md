# Périmètre final proposé du PFE

## Problématique et besoins BeeCoders

Le pilote répond à la question suivante : comment préparer une exploitation de
Moodle qui préserve la qualité de service, réduit les interventions techniques
répétitives et permet une récupération vérifiable des données pédagogiques ?

Le périmètre final proposé couvre trois besoins :

1. détecter une dégradation de Moodle à partir de mesures réelles ;
2. aider l'administrateur à qualifier, diagnostiquer et traiter un incident ;
3. sauvegarder et récupérer les données pédagogiques dans le pilote.

BeeCoders n'exploite pas encore Moodle en production. La solution est donc un
pilote expérimental dans un Codespace, et non une plateforme de production.

## Solution retenue

- Moodle, Apache, PHP-FPM et PostgreSQL sont déployés par Ansible et Docker Compose.
- Prometheus et les exporteurs suivent disponibilité, latence, ressources,
  PHP-FPM, PostgreSQL, conteneurs et fraîcheur de collecte.
- Alertmanager centralise les alertes et peut notifier un relais SMTP configuré hors Git.
- Isolation Forest signale un comportement statistiquement inhabituel.
- FastAPI, PostgreSQL et les workers conservent incidents, diagnostics,
  décisions, interventions et preuves.
- Le dashboard React rassemble les parcours utiles à l'administrateur.
- Les sauvegardes de cours et de site, ainsi que leurs restaurations locales
  existantes, restent accessibles sans nouvelle fonction de sauvegarde.

## Chaîne fonctionnelle principale

| Étape | Composants réels | Résultat attendu |
| --- | --- | --- |
| Supervision | Prometheus, exporteurs, sondes, Alertmanager | Mesures datées, disponibilité et alertes |
| Détection | Règles existantes et Isolation Forest | Observation inhabituelle, sans diagnostic automatique |
| Qualification | Observer et journal PostgreSQL des incidents | Incident confirmé après observations concordantes |
| Diagnostic | API, file PostgreSQL, worker Ansible Runner | Rapport en lecture seule et recommandations |
| Décision | Moteur de décision et politique persistante | Action proposée, autorisée ou refusée avec motif |
| Intervention | Worker borné à PHP-FPM | Une des deux actions prédéfinies seulement |
| Vérification | Sondes HTTPS, métriques et contrôles après action | Rétablissement confirmé ou échec/retour arrière tracé |
| Historique | Journaux PostgreSQL et rapports CSV/HTML | Chronologie consultable par l'administrateur |

Une anomalie Isolation Forest ne saute aucune étape de cette chaîne et ne
déclenche jamais directement une correction.

## Rôle exact d'Isolation Forest

Le modèle utilise les variables temporelles calculées à partir du CPU, de la
mémoire et du disque réellement collectés. L'écran affiche le score, le seuil et
les mesures observées utiles à l'interprétation.

- **Anomalie statistique** : comportement inhabituel par rapport aux données d'entraînement.
- **Incident confirmé** : dégradation confirmée par les règles et observations du pilote.
- **Cause technique** : conclusion d'un diagnostic ; elle n'est pas fournie par le modèle.

Le modèle fonctionne en observation. Sans scénarios indépendamment annotés, son
taux de signalement n'est ni une précision, ni un rappel, ni un taux de panne.
La future évaluation comparera règles classiques et Isolation Forest sur les
mêmes scénarios, avec fausses alertes, précision, rappel et délai de détection.

## Matrice d'audit

| Besoin BeeCoders | Fonctionnalité | État au 15/10/2026 | Modification finale | Preuve attendue |
| --- | --- | --- | --- | --- |
| Détecter | Déploiement Moodle reproductible | Opérationnel dans le Codespace | Conservé | `deploy.sh`, `status.sh`, Moodle HTTP 200 |
| Détecter | Supervision hôte, Moodle, PHP-FPM, PostgreSQL et conteneurs | Opérationnelle | Conservée | Dashboard, Prometheus, fraîcheur des mesures |
| Détecter | Alertes et e-mail | Opérationnels avec relais configuré | Conservés | Alerte de recette et e-mail reçu |
| Détecter | Isolation Forest | Entraîné et affiché sur mesures du pilote | Clarifié ; prévisions retirées | Score, seuil, provenance et rapport d'entraînement |
| Assister | Incidents observés et décisions | Intégrés ; recette complète à répéter | Conservés | Incident, événements, décision et motif cohérents |
| Assister | Diagnostic guidé en lecture seule | Code et worker vérifiés | Conservé | Demande, résultat Runner et lien vers le journal |
| Assister | Redémarrage PHP-FPM arrêté | Déjà démontré sur le pilote | Conservé et borné | Autorisation, contrôles avant/après et durée de reprise |
| Assister | Profil PHP-FPM Normal/Renforcé | Préparé ; bénéfice sous charge non validé | Conservé en mode assisté | File confirmée, action autorisée, p95/erreurs avant-après |
| Récupérer | Sauvegarde/restauration d'un cours | Opérationnelle selon la recette du pilote | Conservée sans extension | Cours copié avec ressource, quiz et devoir |
| Récupérer | Sauvegarde/reprise locale du site | Opérationnelle localement selon la recette du pilote | Conservée sans l'appeler PRA complet | Point, contrôle, reprise locale et RTO mesuré |
| Tracer | Historiques et rapports | Opérationnels | Conservés | Chronologies et exports CSV/HTML |

Les états « opérationnel » reposent sur les recettes déjà exécutées par
l'opérateur. Les autres lignes ne seront déclarées validées qu'après les
scénarios finaux documentés.

## Écarts au cahier des charges initial

| Élément | Statut | Justification |
| --- | --- | --- |
| Socle Moodle, supervision, incidents, diagnostic et traçabilité | Réalisé | Répond aux deux premiers besoins du pilote |
| Sauvegarde/restauration de cours et reprise locale | Réalisé | Répond au besoin pédagogique dans Codespaces |
| Prévisions CPU/RAM/disque à 5/10/15 minutes | Retiré du périmètre final proposé | Réduction du périmètre ; validation obligatoire par les encadrants |
| Autres modèles IA | Retiré du périmètre final proposé | Aucun besoin métier supplémentaire démontré |
| Gestion fonctionnelle multi-rôles | Retirée du périmètre final proposé | Le pilote final a un administrateur BeeCoders |
| Redis | Reporté | Non nécessaire aux trois scénarios retenus |
| Haute disponibilité et orchestration multi-serveurs | Reportées | Hors capacité d'un pilote Codespaces |
| PRA sur infrastructure indépendante et copie hors site | Reporté | La reprise actuelle utilise le même Codespace |
| Déploiement sur les serveurs BeeCoders | Reporté | À réaliser après validation du pilote |

Le retrait des prévisions est une **demande de modification du périmètre**. Il
ne doit pas être présenté comme une conformité au cahier des charges initial.
Leur expérimentation reste consultable dans l'historique Git, mais elle n'est
plus entraînée, déployée ni proposée à l'administrateur.

## Architecture et limites

Le navigateur accède au dashboard via le proxy Codespaces. Le dashboard appelle
FastAPI. Les lectures opérationnelles proviennent de Prometheus ; les états
durables sont conservés dans le PostgreSQL du pilotage, séparé de la base Moodle.
Les workers exécutent les diagnostics en lecture seule et les deux interventions
PHP-FPM autorisées. Les secrets restent hors du dépôt.

Limites assumées : une seule instance Codespaces, un seul administrateur de
démonstration, dépôt Restic local, aucune haute disponibilité, aucun stockage
hors site et aucune généralisation du modèle à la production. Les mécanismes
automatiques restent présents mais la politique finale privilégiée est
Observation ou Assisté ; ils ne sont pas activés par ce lot.

## Mises à jour académiques à prévoir

Lorsque le rapport et le cahier des charges éditable seront fournis, mettre à jour :

- le périmètre et les cas d'utilisation ;
- l'architecture IA et la description du pipeline ;
- la partie évaluation, limitée à Isolation Forest ;
- les diagrammes montrant les anciens endpoints de prévision ;
- les résultats des trois scénarios, RTO et mesures avant/après ;
- les limites, écarts validés par les encadrants et perspectives BeeCoders.
