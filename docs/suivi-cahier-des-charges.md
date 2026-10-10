# Suivi technique du cahier des charges valide

Ce suivi decrit les fonctions et leurs preuves. Le cahier des charges envoye
aux encadrants est la reference et n'est pas modifie par les changements de code.

| Exigence | Sources principales | Etat et preuve a presenter |
|---|---|---|
| Deploiement reproductible Moodle et HTTPS | Roles Ansible `moodle`, `runtime`, `linux` | Deploiements et secondes executions sans changement observes sur le pilote. |
| Sauvegarde coherente et PRA | Roles `operations`, `recovery`, Restic et rclone | Scripts presents et sauvegarde du 08/10 reussie ; conserver les preuves des restaurations completes et des RTO/RPO. |
| Suivi des sauvegardes | Collecteur backup_status, API et interface React | Deploiement et affichage verifies par l'operateur le 08/10 ; seconde execution sans changement ; fusion main a04e5cb. La fraicheur ne prouve pas l'integrite du depot. |
| Protection de PostgreSQL du pilotage | Collecteur pilotage_backup, role Ansible et Restic | Sauvegarde du 09/10 confirmee ; restauration isolee de 16 tables et 415 lignes validee en 76,819 s, sequences verifiees, production inchangee. Suivi affiche, CI reussie et fusion main confirmes par l'operateur. Cette preuve porte sur le pilotage, pas sur un PRA Moodle complet. |
| Supervision centralisee | Roles `monitoring`, `application_monitoring`, API des mesures | Ressources Linux et controle HTTPS disponibles. PHP-FPM et PostgreSQL affiches sur le pilote ; correctif FastCGI borne, CI reussie et fusion main b55c76d confirmes par l'operateur le 09/10. Indicateurs de conteneurs, Redis et Alertmanager restent a completer. |
| Catalogue des points de reprise | `backup_status/catalog_collector.py`, role `backup_catalog`, API et React | Catalogue Moodle affiche sur le pilote, seconde execution sans changement, CI complete verte et fusion main 20336ed confirmes par l'operateur le 09/10. Deux scopes, references completes et dates distinguees ; tests Restic reels en Python 3.10 et 3.12 passes en CI. La presence d'un point ne prouve pas sa restauration. |
| Demandes et approbation de restauration | `pilotage/recovery.py`, API, role `recovery_requests`, React | Ce lot prepare le journal PostgreSQL, les demandes de tests isoles et les decisions administrateur avec audit. Tests locaux et couverture PostgreSQL reelle en CI ; deploiement, recette et CI de ce nouveau lot encore a confirmer. L'execution des tests depuis ces demandes et la reprise de production restent a raccorder. |
| Tableau de bord React et API | `dashboard`, `control-plane` | Interface et API deployees. Catalogue fusionne ; parcours de demandes ajoute dans ce lot, a valider sur le pilote. Les politiques de correction restent a completer. |
| Historique PostgreSQL separe de Moodle | `pilotage`, role `control_plane_database` | Migration activee sur le pilote ; resume des incidents, observateur et ancienne correction PHP consultables apres la bascule. |
| Anomalies et previsions 5/10/15 minutes | `ml`, `app/anomalies.py`, `app/forecasts.py` | Modeles experimentaux et rejeu historique testes. Donnees annotees et comparaison aux references restent a completer ; Ridge ne depasse pas actuellement la persistance sur le CPU. |
| Incidents, decisions et diagnostics | `app/incidents.py`, `app/decisions.py`, `diagnostics` | Incidents confirmes et persistants, decisions en simulation et diagnostics Runner. Persistances des decisions et actions depuis l'interface a completer. |
| Corrections encadrees | `remediation`, politiques, scripts de validation | Remise en service PHP validee manuellement en 66,385 s le 07/10 puis automatiquement en 86,237 s sans secours le 10/10. Politiques fusionnees et CI verte confirmees. Profils PHP-FPM 5/8, autorisations, retour precedent et affichage implementes et verifies localement dans ce lot ; deploiement a confirmer. Essais de charge et repetitions reportes a la recette finale. |
| Authentification administrateur/lecteur | `pilotage/auth.py`, `app/auth.py`, role `authentication`, composants React | Comptes et roles testes par l'operateur ; corrections des sessions concurrentes et delais de tests poussees, CI reussie le 08/10. |
| Verification continue | `.github/workflows/ci.yml`, `ci` | Tests Python, React, syntaxe Ansible, images, stockage PostgreSQL 16 et exporteurs applicatifs reels valides dans les lots precedents. Ce lot ajoute le test du catalogue avec Restic et un depot temporaire. |
| Validation finale et livrables | Tests de charge, scenarios documentes, rapport | Completer les charges authentifiees, deux familles de corrections repetees, deux PRA complets, UML, runbooks, rapport et presentation. |

Ordre de travail : deployer les profils sans essais de panne ou de charge ;
completer la supervision et les autres exigences restantes ; recette mesuree finale ;
rapport et presentation. Les interruptions
locales de collecte sont conservees comme donnees manquantes, sans interpolation
pour rendre artificiellement disponibles les modeles.
