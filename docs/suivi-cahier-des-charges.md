# Suivi technique du cahier des charges valide

Point de reprise du 11/10/2026 : `main` est au merge de la PR #10,
`83418144531552ce9ba735dfdd8dc17421693a5b`. Les onze controles CI de ce commit
sont reussis. Le cahier des charges fourni aux encadrants reste la reference.
Une fonction presente dans le code et testee en CI ne constitue pas a elle
seule une recette sur la VM ou une mesure des objectifs du PFE.

| Exigence | Sources principales | Etat et preuve a presenter |
| --- | --- | --- |
| Deploiement reproductible Moodle et HTTPS | Roles Ansible `moodle`, `runtime`, `linux` | Deploiements et secondes executions sans changement deja observes sur le pilote. Recette des parcours pedagogiques authentifies a consigner. |
| Sauvegarde coherente et PRA | Roles `operations`, `recovery`, Restic et rclone | Sauvegarde complete et controle d'integrite documentes. Deux restaurations Moodle completes, verifications de cours/fichiers/resultats et mesures RTO/RPO restent a fournir. Confirmer aussi la copie hors-site. |
| Suivi des sauvegardes | `backup_status`, API et React | Deploiement et affichage verifies par l'operateur ; fusion deja confirmee. La fraicheur ne prouve pas l'integrite du depot. |
| Protection PostgreSQL du pilotage | `pilotage_backup`, Ansible et Restic | Restauration isolee de 16 tables et 415 lignes validee en 76,819 s le 09/10. Verifier maintenant le profil etendu couvrant les demandes et executions ; cette preuve ne remplace pas un PRA Moodle. |
| Supervision Linux, HTTPS et applicative | `monitoring`, `application_monitoring`, API | Deployee sur le pilote. PHP-FPM et PostgreSQL verifies dans les lots precedents. |
| Supervision des conteneurs et navigation | `container_monitoring`, Node Exporter, API et React | PR #9 fusionnee sur main et CI complete verte. Le correctif des labels, les trois services Moodle et la navigation par vues sont deja livres ; conserver la verification finale sur la VM. |
| Alertes et notifications | Role `monitoring`, [guide Alertmanager](alertmanager.md), tests CI | PR #10 fusionnee et CI verte. Recette VM du 11/10 : premier passage ok=21 changed=8 failed=0, second passage ok=19 changed=0 failed=0 ; 16 regles, aucune erreur, aucune alerte active. Interface Alertmanager accessible en version 0.34.1. SMTP reste desactive ; une notification email reelle n'est pas encore validee. |
| Catalogue des points de reprise | `backup_status/catalog_collector.py`, role `backup_catalog`, API et React | Catalogue affiche sur le pilote, second passage sans changement et fusion deja confirmes. Les tests Restic reels passent en CI sous Python 3.10 et 3.12. La presence d'un point ne prouve pas sa restauration. |
| Demandes et execution de tests isoles | `pilotage/recovery.py`, `pilotage/recovery_execution.py`, `recovery_tests`, API et React | Demandes, approbations, file persistante et bouton de lancement sont deja presents dans main. Le worker Runner et les conteneurs de test sont couverts par la CI verte. Deploiement et essais avec des sauvegardes reelles a confirmer ; reprise de production a exercer separement. |
| Tableau de bord, acces et rapports | `dashboard`, `control-plane`, `pilotage/auth.py` | API, roles administrateur/lecteur, politiques, chronologies et exports CSV/HTML presents. Ce lot protege son propre compte contre la desactivation et la perte du role admin, dans l'API et l'interface. Recette VM et parcours complets a confirmer. |
| Historique PostgreSQL separe de Moodle | `pilotage`, [journal des decisions](decision-journal.md) | Migration des incidents et operations deja active. Ce lot ajoute les transitions de decisions non executees, leurs mesures, motifs, contexte/modeles et politique complete, ainsi que consultation et exports. Ecriture atomique par l'observateur ; sauvegarde et restauration etendues aux trois nouvelles tables. Merge, CI du lot et recette VM restent a confirmer. |
| Anomalies et previsions 5/10/15 minutes | `ml`, `app/anomalies.py`, `app/forecasts.py` | Modeles experimentaux et rejeu historique testes. Collecte annotee, comparaison seuils/persistance/tendance et publication des resultats encore a completer. Ridge ne depasse pas actuellement la persistance sur le CPU. |
| Corrections encadrees | `remediation`, politiques, scripts de validation | Remise en service PHP validee en 66,385 s manuellement puis en 86,237 s automatiquement. Profils Normal 5 / Renforce 8 deja livres par PR #8. Ne pas les reimplementer ; effectuer les essais sous charge et trois repetitions par famille avec controles fonctionnels. |
| CI et livraison | `.github/workflows/ci.yml`, `ci` | Tests Python/React/Ansible, PostgreSQL 16, exporteurs, Restic et restauration fictive verts sur main. Le lot Alertmanager ajoute un controle bloquant avant construction des images. Construction d'images presente ; publication et procedure de livraison identifiee a formaliser pour la recette. |
| Redis | Technologie du cahier des charges | Reporte explicitement par l'operateur. Aucun ajout dans le lot Alertmanager ; decision a tracer avant la recette finale du perimetre. |
| Validation et livrables finaux | Locust, dossier experimental, UML, rapport, presentation | Charges authentifiees, comparaisons, deux PRA complets, guides consolides, dossier de conception et soutenance restent a achever ou a verifier hors de ce depot. |

## Travail restant apres le journal des decisions

1. Fusionner et deployer ce lot, verifier le second passage sans changement,
   les comptes personnels, les transitions et leurs exports ; effectuer une
   sauvegarde/restauration du profil etendu. Alertmanager est deja deploye :
   configurer et tester le relais email si les notifications externes sont retenues.
2. Terminer les parcours de diagnostic et de correction depuis l'interface
   lorsqu'ils sont attendus, puis effectuer la recette
   des droits, approbations, suspensions, doublons et mesures inconnues.
3. Effectuer les charges Locust authentifiees et constituer un jeu de donnees
   annote. Mesurer p95, taux d'erreurs, precision/rappel/fausses alertes et
   delai de detection ; evaluer les previsions par horizon contre les references.
4. Repeter au moins trois fois chacune des deux familles de correction,
   documenter les echecs, retours au profil precedent et controles apres action.
5. Realiser deux PRA Moodle complets sur un environnement distinct, verifier
   cours/fichiers/resultats fictifs, mesurer RTO/RPO et confirmer la copie hors-site.
6. Formaliser les images et livraisons identifiees, consolider UML, architecture,
   guides et preuves, puis terminer le rapport, la presentation et la demonstration.

## Estimation de progression

Estimation de suivi, pas un comptage de fichiers ni une mesure d'heures :
environ **80 % des composants techniques** sont prepares apres ce lot,
et environ **65 % du PFE global** sont etayes par les elements disponibles.
L'ecart correspond notamment a la validation experimentale, au PRA complet,
a la livraison finale et aux livrables academiques. Le rapport et les diagrammes
eventuellement disponibles hors de ce depot peuvent modifier cette estimation.
Le journal des decisions ne peut etre declare deploye sur le pilote qu'apres
merge, CI verte et recette VM. Redis reste explicitement reporte.
