# Historique persistant des decisions

Ce lot complete l'exigence 4.2 du cahier des charges : conserver les mesures,
la justification et les versions utilisees pour les decisions, y compris les
recommandations non executees. Les incidents, diagnostics, remediations,
approbations et politiques deja livres conservent leurs journaux propres.

## Enregistrement

L'observateur, cadence a 30 secondes, evalue les incidents apres chaque
collecte complete. Les incidents et leurs decisions sont ecrits dans une seule
transaction PostgreSQL. Si le journal echoue, le cycle entier est annule ;
une collecte ne devient pas valide sans son audit. Les lectures HTTP
n'initialisent aucun schema et n'ecrivent aucune decision.

Une entree est creee au changement d'etat, de motif, de contexte ou de modele,
de priorite, d'action proposee ou de version/empreinte de politique. Elle
conserve la decision exacte, ses mesures, ses motifs, son identifiant, le
catalogue complet de la politique et l'etat de l'observateur. Le contexte
porte notamment l'identifiant du modele lorsqu'un modele participe au signal.

Une recommandation identique repetee prolonge la derniere confirmation et
incremente le nombre d'observations. Ses premieres mesures ne sont pas
remplacees par les valeurs suivantes. Ce journal de transitions ne constitue
pas une copie de toutes les series temporelles Prometheus. Les etats
`recommendation`, `blocked`, `monitoring` et `closed` sont conserves. La cloture
est enregistree dans le cycle qui resout l'incident.

Le schema additionnel `pilotage_decisions` v1 contient `schema_info`, `entries`
et `state`. L'initialisation est transactionnelle et idempotente, sous le
meme verrou PostgreSQL que l'observateur. Les schemas existants restent v1.
Aucun historique anterieur a l'activation n'est reconstruit. Les incidents
deja ouverts sont pris en compte au prochain cycle complet. Lorsque
l'observateur est arrete, aucune evaluation nouvelle n'est inventee ; la
couverture du journal passe a obsolete apres 90 secondes.

## Consultation et exports

Le menu **Historique des decisions** affiche les transitions, les mesures
de leur premiere evaluation et la periode de confirmation. Les filtres
portent sur l'etat et l'identifiant d'incident. Chaque page (50 entrees)
peut etre exportee en CSV ou HTML avec ses preuves et son perimetre explicite.
L'export neutralise les formules CSV et echappe le HTML.

| Lecture | Parametres |
| --- | --- |
| `GET /api/v1/decisions/history` | `limit` 1..100, `before_id`, `status`, `incident_id` |
| `GET /api/v1/decisions/history/{entry_id}` | Identifiant positif de l'entree |

La pagination descend par identifiant ; les nouvelles entrees ne deplacent
pas les pages suivantes. Les champs `coverage` rendent explicites le debut
de couverture et la derniere collecte. Les lecteurs et administrateurs
authentifies peuvent consulter. Le proxy autorise uniquement GET sur ces
routes et aucun bouton d'execution n'est ajoute au journal.

Une decision archivee est une preuve historique, jamais une autorisation
d'execution. Les workers revalident toujours les mesures courantes, les
droits, la politique, les limites de tentatives et les approbations existantes.

## Installation sur le pilote

Prerequis : main apres la PR #10, PostgreSQL du pilotage actif et CI verte.
Redis reste reporte. Le groupe Moodle active `control_plane_decision_journal_enabled`.
Le playbook met a jour les scripts de sauvegarde, l'API, l'observateur et le
tableau de bord. L'observateur initialise les trois nouvelles tables. Moodle
et Alertmanager n'ont pas besoin d'etre redeployes pour ce lot.

```sh
ansible-playbook ansible/playbooks/decision-journal.yml -K
```

Relancer la meme commande pour verifier `changed=0`, `failed=0`. Le controle
Ansible exige un journal accessible et une collecte courante ; une liste
vide est normale en l'absence d'incident. `storage/health` indique le backend
PostgreSQL et la version 1 du journal.

Recette : ouvrir le nouveau menu, consulter un incident existant, attendre
deux collectes identiques puis verifier une seule transition et plusieurs
confirmations. Exporter une page et verifier mesures/politique. Dans
**Comptes et acces**, les controles de son propre compte sont desactives ;
un PATCH direct de desactivation ou de changement de role renvoie HTTP 409.
Un autre administrateur peut gerer ce compte, sous la protection du dernier
administrateur actif. Le changement de mot de passe personnel reste disponible.

## Sauvegarde et validation

Les nouvelles sauvegardes detectent automatiquement `pilotage_decisions` et
verifient ses trois tables, leurs colonnes, comptages, empreintes et sequence
d'identifiants. Les anciens profils de sauvegarde (16, 19, 21 ou 28 tables)
restent restaurables. Le profil complet avec politiques et decisions couvre
31 tables. Un test PostgreSQL 16 utilise de vraies decisions de l'observateur,
un instantane exporte, pg_dump/pg_restore et la verification de la sequence.

Les tests du lot couvrent aussi repetitions, changements de politique,
blocage sur mesure inconnue, cloture, nouvel episode, redemarrage du lecteur,
absence d'historique fabrique, annulation atomique sur echec, pagination,
lectures HTTP sans ecriture et refus d'auto-desactivation avec deux admins.
La recette VM et le second passage Ansible doivent etre consignes apres merge.
