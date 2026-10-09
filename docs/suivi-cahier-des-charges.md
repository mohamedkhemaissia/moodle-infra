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
| Catalogue des points de reprise | `backup_status/catalog_collector.py`, role `backup_catalog`, API et React | Deux scopes, references completes, dates d'archivage et de donnees distinguees, limites et permissions testees localement. Job Restic reel ajoute a la CI ; recette VM et resultat de ce nouveau job encore a confirmer. Aucune restauration ajoutee. |
| Tableau de bord React et API | `dashboard`, `control-plane` | Interface et API deployees. Ce lot ajoute le catalogue consultable par les roles lecteur et administrateur ; politiques et demandes de restauration a completer. |
| Historique PostgreSQL separe de Moodle | `pilotage`, role `control_plane_database` | Migration activee sur le pilote ; resume des incidents, observateur et ancienne correction PHP consultables apres la bascule. |
| Anomalies et previsions 5/10/15 minutes | `ml`, `app/anomalies.py`, `app/forecasts.py` | Modeles experimentaux et rejeu historique testes. Donnees annotees et comparaison aux references restent a completer ; Ridge ne depasse pas actuellement la persistance sur le CPU. |
| Incidents, decisions et diagnostics | `app/incidents.py`, `app/decisions.py`, `diagnostics` | Incidents confirmes et persistants, decisions en simulation et diagnostics Runner. Persistances des decisions et actions depuis l'interface a completer. |
| Corrections encadrees | `remediation`, `scripts/validate_php_recovery.py` | Remise en service PHP validee manuellement en 66,385 s, sans secours, le 07/10. Ajouter les profils PHP-FPM et les politiques assistees/automatiques ; mesurer les repetitions. |
| Authentification administrateur/lecteur | `pilotage/auth.py`, `app/auth.py`, role `authentication`, composants React | Comptes et roles testes par l'operateur ; corrections des sessions concurrentes et delais de tests poussees, CI reussie le 08/10. |
| Verification continue | `.github/workflows/ci.yml`, `ci` | Tests Python, React, syntaxe Ansible, images, stockage PostgreSQL 16 et exporteurs applicatifs reels valides dans les lots precedents. Ce lot ajoute le test du catalogue avec Restic et un depot temporaire. |
| Validation finale et livrables | Tests de charge, scenarios documentes, rapport | Completer les charges authentifiees, deux familles de corrections repetees, deux PRA complets, UML, runbooks, rapport et presentation. |

Ordre restant : valider la recette du catalogue ;
politiques et reprise applicative ; modes et seconde famille
de corrections ; validation mesuree ; rapport et presentation. Les interruptions
locales de collecte sont conservees comme donnees manquantes, sans interpolation
pour rendre artificiellement disponibles les modeles.
