# Gestion persistante des incidents observés

La version 0.4.0 de l’API ajoute un historique d’incidents. Un processus `observer`, séparé de l’API, interroge Prometheus et le modèle d’anomalies toutes les 30 secondes. Il fonctionne uniquement en observation.

## Signaux suivis

| Signal | Ce qu’il décrit |
| --- | --- |
| Lecture Prometheus | Échec de connexion ou réponse inexploitable. |
| Collecte Linux | Collecte absente, arrêtée, ancienne ou ressources incomplètes. |
| Collecte Moodle | Collecte de la sonde absente, arrêtée, ancienne ou résultat incomplet. |
| Sonde HTTPS Moodle | Échec observé par la sonde de la page de connexion, lorsque sa collecte est exploitable. |
| CPU, mémoire, disque | Dépassement des seuils expérimentaux des ressources serveur. |
| Modèle d’anomalies | Artefacts configurés mais incompatibles ou indisponibles. |
| Historique d’analyse | Ressources ne présentant pas les 420 secondes d’historique continu nécessaires. |
| Anomalie statistique | Score Isolation Forest supérieur au seuil du modèle ; aucune cause de panne établie. |

Ces signaux restent distincts. Une interruption de collecte ne prouve pas une panne Moodle. Les prévisions ne déclenchent pas d’incident de saturation dans cette version.

## Cycle de vie

Deux contrôles déclenchants consécutifs sur des points différents de la grille ouvrent un incident. Trois contrôles consécutifs de retour à la normale le résolvent. Les alertes répétées alimentent le même épisode. Une nouvelle alerte après sa résolution crée un nouvel épisode.

Une valeur inconnue interrompt les compteurs de confirmation et de récupération, et conserve un incident ouvert. Une observation dans la zone d’hystérésis conserve également l’incident ouvert. Un intervalle supérieur à 90 secondes réinitialise les compteurs ; il ne constitue pas une preuve de récupération. Un point déjà traité ou plus ancien est ignoré. Les compteurs sont conservés au redémarrage.

Le journal enregistre l’ouverture, la résolution, la perte et la reprise d’observation, les interruptions de cadence et les changements de contexte du modèle. Il n’ajoute pas un événement identique à chaque contrôle.

## Seuils expérimentaux

| Signal | Déclenchement | Retour à la normale |
| --- | --- | --- |
| CPU | ≥ 85 % | ≤ 75 % |
| Mémoire | ≥ 90 % | ≤ 85 % |
| Disque disponible | ≤ 10 % | ≥ 15 % |
| Score statistique | > seuil du modèle | ≤ seuil du modèle − 0,02, avec minimum 0 |

Les seuils et les durées de confirmation sont des paramètres initiaux à valider avec BeeCoders. Ils ne prouvent pas la précision de la détection. L’observateur actuel utilise la grille de 30 secondes du modèle déjà entraîné.

## Stockage et consultation

SQLite conserve les épisodes, les états des signaux et les événements dans `/var/lib/moodle-control-plane/incidents/incidents.sqlite3` sur le serveur. Ce répertoire est indépendant des images et des modèles ML. L’observateur détient un verrou exclusif empêchant deux observateurs d’écrire simultanément. Chaque cycle est une transaction atomique. La journalisation utilise le mode rollback `DELETE` et la synchronisation `FULL`.

L’API monte ce répertoire en lecture seule et ouvre des connexions SQLite en lecture seule. Lire un endpoint ne collecte pas de nouvelles observations et ne modifie aucun incident. La santé de l’observateur est publiée séparément de celle des sources surveillées. Un observateur trop ancien ou une incohérence d’horloge apparaissent explicitement dans le résumé.

| Endpoint GET | Résultat |
| --- | --- |
| `/api/v1/incidents/summary` | Comptages, état de l’observateur, état des dix signaux et politique. |
| `/api/v1/incidents?status=open&limit=20` | Liste paginée des épisodes ouverts. Le filtre `resolved` est également disponible. |
| `/api/v1/incidents/{id}` | Détail d’un épisode, preuves et dates. |
| `/api/v1/incidents/{id}/events?after_id=0&limit=50` | Chronologie paginée des événements. |

Un stockage non configuré, absent ou incompatible renvoie HTTP 503 ; un identifiant inconnu renvoie HTTP 404. Une liste vide est un résultat valide lorsqu’aucun signal n’a encore été confirmé.

## Portée

Le stockage survit aux redémarrages et aux remplacements des conteneurs tant que le répertoire persistant est conservé. Il ne constitue pas une sauvegarde hors site. Son intégration au PRA et l’étude d’une base PostgreSQL pour une architecture avec plusieurs instances restent à réaliser. SQLite doit être utilisé ici sur un système de fichiers local, avec cet observateur unique.

Le module n’exécute aucune commande d’administration de Moodle, n’envoie aucune notification extérieure et ne comporte pas encore d’acquittement manuel. La remédiation et ses autorisations seront développées dans les étapes suivantes. Les tests techniques ne remplacent pas une évaluation sur des incidents réels annotés.

## Références techniques

- [Verrouillage et transactions SQLite](https://www.sqlite.org/lockingv3.html).
- [Module sqlite3 de Python](https://docs.python.org/3.10/library/sqlite3.html).
