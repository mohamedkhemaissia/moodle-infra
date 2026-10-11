# Moteur de décision en simulation

L’API calcule des recommandations à partir des incidents persistants. Ce moteur utilise des règles explicites. Isolation Forest fournit un signal expérimental distinct ; il ne choisit aucune correction et ne détermine aucune cause de panne.

## Du signal à la recommandation

Les incidents, les onze signaux et le dernier passage de l’observateur sont lus dans une même transaction en lecture seule (PostgreSQL sur le pilote, SQLite pendant la transition). Cette lecture reste cohérente lorsqu’un nouveau cycle de l’observateur est écrit simultanément.

Avant de proposer un diagnostic, le moteur vérifie :

- Un passage de l’observateur récent, une horloge cohérente et tous les signaux traités.
- Un même instant d’observation pour les signaux et l’incident concerné.
- Un signal toujours déclenché et confirmé deux fois dans le contexte courant.
- Des dépendances de collecte exploitables et des preuves compatibles avec le signal.

Après une interruption de cadence ou un changement de contexte, une nouvelle confirmation est nécessaire. Une observation inconnue suspend la recommandation. Un retour à la normale en cours ou une observation dans la zone d’hystérésis conduisent à poursuivre la surveillance. Un épisode résolu ne propose aucune action courante.

## Recommandations

| Incident | Diagnostic proposé |
| --- | --- |
| Lecture de Prometheus | Vérifier les cibles, les journaux et la connectivité. |
| Collecte Linux | Vérifier Node Exporter, les heures de collecte et les interruptions. |
| Collecte de la sonde Moodle | Vérifier Blackbox, le nom, le certificat et la configuration. |
| Modèle d’anomalies | Vérifier empreintes, provenance et compatibilité. |
| Historique insuffisant | Stabiliser la collecte et accumuler la durée continue demandée par le modèle. |
| Sonde HTTPS en échec | Examiner HTTPS et les services Apache, PHP-FPM et PostgreSQL pour identifier la cause. |
| CPU élevé | Identifier les consommateurs, quotas, tâches et charge PHP-FPM. |
| Mémoire élevée | Examiner consommateurs, swap et événements OOM. |
| Disque disponible faible | Examiner l’occupation et la rétention avant toute suppression. |
| Anomalie statistique | Examiner le contexte expérimental et les mesures opérationnelles. |
| File PHP-FPM persistante | Examiner la capacité et les observations du pool avant un changement de profil. |

Les incidents de qualité des données sont prioritaires, puis viennent la disponibilité, le disque, la mémoire et le CPU, et les anomalies statistiques. Les épisodes liés sont référencés ; leur coexistence ne constitue aucune preuve de causalité.

## Endpoints GET

| Endpoint | Contenu |
| --- | --- |
| `/api/v1/decisions/summary` | Recommandations pour les épisodes ouverts et comptages par état. |
| `/api/v1/decisions/{incident_id}` | Recommandation pour un épisode, y compris son état résolu. |
| `/api/v1/decisions/policies` | Catalogue des règles et empreinte de la politique. |
| `/api/v1/decisions/history` | Transitions persistantes, filtres par état et incident, pagination par identifiant. |
| `/api/v1/decisions/history/{entry_id}` | Mesures, justification, politique et observateur archivés pour une transition. |

Les états sont `recommendation`, `blocked`, `monitoring` et `closed`. Les codes de raison expliquent les conditions manquantes. `available` indique que le calcul de la réponse fonctionne ; `snapshot_usable` décrit séparément l’état des observations. Une liste vide est valide lorsque tous les épisodes sont résolus.

Chaque décision comporte un identifiant déterministe, la version et l’empreinte de sa politique, ses preuves et les épisodes liés. Sa validité se termine 90 secondes après la date des données utilisées. Relire la réponse ne prolonge pas cette validité. Les réponses portent `Cache-Control: no-store`.

## Exemple du pilote

L’incident `analysis_history` demande 420 secondes d’historique continu. Lorsque l’observation est récente et confirmée, l’action proposée est `wait_contiguous_history`. Si l’observateur est ancien, la décision devient `blocked`, avec la raison `observer_stale` et aucune recommandation courante.

## Déploiement et portée

`control_plane_decisions_enabled` active le moteur et exige que les incidents soient activés. Le groupe Moodle reçoit cette variable dans `ansible/inventories/group_vars/moodle/decisions.yml`. Ansible reconstruit l’image, exécute les tests et vérifie le nouvel endpoint en mode simulation. Les délais et la correction du contrôle de santé précédemment installés sont conservés.

Les lectures de décision n’interrogent pas Prometheus et n’écrivent ni incidents ni événements. Elles ne lancent aucune commande, notification ou exécution Ansible. L’évaluation actuelle reste calculée à la lecture ; son champ `persisted:false` décrit cette réponse exacte. L’observateur conserve séparément les transitions significatives dans le [journal des décisions](decision-journal.md).

Les diagnostics, corrections, autorisations et journaux d’exécution existent déjà dans les modules dédiés. Une ancienne décision archivée ne remplace jamais leur revalidation des mesures, de la politique et des droits avant une action.

## Références techniques

- [Transactions et verrouillage SQLite](https://www.sqlite.org/lockingv3.html).
- [Validation des paramètres numériques FastAPI](https://fastapi.tiangolo.com/tutorial/path-params-numeric-validations/).
- [Interface et artefacts d’Ansible Runner, pour l’étape suivante](https://docs.ansible.com/projects/runner/en/stable/intro/).
