# Suivi technique du cahier des charges valide

Ce suivi decrit les fonctions et leurs preuves. Le cahier des charges envoye
aux encadrants est la reference et n'est pas modifie par les changements de code.

| Exigence | Sources principales | Etat et preuve a presenter |
|---|---|---|
| Deploiement reproductible Moodle et HTTPS | Roles Ansible `moodle`, `runtime`, `linux` | Deploiements et secondes executions sans changement observes sur le pilote. |
| Sauvegarde coherente et PRA | Roles `operations`, `recovery`, Restic et rclone | Scripts presents et sauvegarde du 08/10 reussie ; conserver les preuves des restaurations completes et des RTO/RPO. |
| Supervision centralisee | Role `monitoring`, `app/prometheus.py` | Ressources Linux et controle HTTPS disponibles. Ajouter les indicateurs de conteneurs, PHP-FPM et PostgreSQL. |
| Tableau de bord React et API | `dashboard`, `control-plane` | Interface et API deployees ; ce lot ajoute connexion et gestion des comptes. Gestion des sauvegardes a completer. |
| Historique PostgreSQL separe de Moodle | `pilotage`, role `control_plane_database` | Migration activee sur le pilote ; resume des incidents, observateur et ancienne correction PHP consultables apres la bascule. |
| Anomalies et previsions 5/10/15 minutes | `ml`, `app/anomalies.py`, `app/forecasts.py` | Modeles experimentaux et rejeu historique testes. Donnees annotees et comparaison aux references restent a completer ; Ridge ne depasse pas actuellement la persistance sur le CPU. |
| Incidents, decisions et diagnostics | `app/incidents.py`, `app/decisions.py`, `diagnostics` | Incidents confirmes et persistants, decisions en simulation et diagnostics Runner. Persistances des decisions et actions depuis l'interface a completer. |
| Corrections encadrees | `remediation`, `scripts/validate_php_recovery.py` | Remise en service PHP validee manuellement en 66,385 s, sans secours, le 07/10. Ajouter les profils PHP-FPM et les politiques assistees/automatiques ; mesurer les repetitions. |
| Authentification administrateur/lecteur | `pilotage/auth.py`, `app/auth.py`, role `authentication`, composants React | Comptes PostgreSQL, sessions revocables, droits controles cote API et gestion dans l'interface prepares dans ce lot ; validation CI puis activation sur le pilote requises. |
| Verification continue | `.github/workflows/ci.yml`, `ci` | Tests Python, React, syntaxe Ansible et images. Ce lot ajoute la verification du stockage sur PostgreSQL 16 reel. |
| Validation finale et livrables | Tests de charge, scenarios documentes, rapport | Completer les charges authentifiees, deux familles de corrections repetees, deux PRA complets, UML, runbooks, rapport et presentation. |

Ordre restant : verifier et activer les comptes ;
observabilite applicative ; sauvegardes dans le pilotage ; modes et seconde famille
de corrections ; validation mesuree ; rapport et presentation. Les interruptions
locales de collecte sont conservees comme donnees manquantes, sans interpolation
pour rendre artificiellement disponibles les modeles.
