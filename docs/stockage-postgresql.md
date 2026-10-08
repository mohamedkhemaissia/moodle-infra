# Stockage PostgreSQL du pilotage

Le cahier des charges demande une base propre au pilotage, separee de celle de
Moodle. Ce lot migre les historiques d'incidents, les diagnostics Runner et
l'audit des corrections. Les endpoints et les regles metier restent compatibles
avec le tableau de bord actuel.

## Organisation

- Serveur PostgreSQL 16 distinct, projet Compose `moodle-control-plane-db`.
- Base `moodle_pilotage`, compte `pilotage` sans droits superuser ni creation
  de roles/bases. Le serveur ecoute uniquement sur `127.0.0.1:15432`.
- Schemas `pilotage_incidents`, `pilotage_diagnostics`, `pilotage_remediation`.
- Donnees dans `/opt/moodle-control-plane-db/data`, independantes de Moodle.
- Configuration dans `/etc/moodle-pilotage/database.json`, mot de passe dans
  un fichier protege ; aucun secret dans Git ou dans les variables Compose.
- La version du schema est verifiee. Une panne PostgreSQL produit une erreur
  de stockage ; aucun repli automatique vers un ancien historique SQLite.

Les transactions serialisent les transitions d'incidents et les reservations de
corrections entre processus. Les lectures portant sur plusieurs requetes utilisent
un instantane coherent. Les delais de connexion, de requete et de verrou sont bornes.

## Deploiement sur le pilote existant

Prerequis : depot propre sur la branche de ce lot, pilote actuel fonctionnel,
variables existantes d'incidents/diagnostics/corrections et artefacts ML conserves.
La CI de la branche doit etre verte avant execution. Ce playbook utilise le Python
3.12 et Runner deja installes et n'arrete aucun service pedagogique Moodle.

```bash
ansible-playbook ansible/playbooks/control-plane-database.yml -K
```

Le playbook met a jour les commandes locales, demarre le nouveau PostgreSQL,
arrete l'observateur, puis copie les trois journaux dans une transaction. La copie
conserve les IDs, evenements, preuves JSON, compteurs, executions et reservations.
Les valeurs relues dans PostgreSQL sont comparees aux valeurs sources avant COMMIT.
Les sequences conservent aussi les IDs deja attribues puis supprimes.

Les fichiers sources restent a leur emplacement. Le marqueur
`/etc/moodle-pilotage/active` est publie uniquement apres la verification et le
COMMIT, pendant que les trois verrous d'ecriture sont encore detenus. Les commandes
locales lisent ce marqueur au lancement ; l'API et l'observateur sont ensuite
redeployes avec la meme configuration PostgreSQL. L'API garde ses montages SQLite
pour la transition, mais lit PostgreSQL lorsque cette configuration est active.

La migration refuse un ecrivain actif, un journal incompatible, une execution
`running` ou une cible preexistante non reconnue. Une execution `running` ancienne
necessite une verification operateur, sans modification automatique de son statut.
Une erreur de copie annule toutes les insertions et creations de schemas ;
l'observateur est relance avec sa configuration precedente. Une reprise reconnue
par empreinte ne recopie pas les donnees et preserve les nouvelles ecritures PG.

## Verification apres deploiement

```bash
sudo docker compose -p moodle-control-plane-db -f /opt/moodle-control-plane-db/compose.yml ps
curl --fail --silent --show-error http://127.0.0.1:8000/api/v1/storage/health | python3 -m json.tool
curl --fail --silent --show-error http://127.0.0.1:8000/api/v1/incidents/summary | python3 -m json.tool
curl --fail --silent --show-error http://127.0.0.1:8000/api/v1/diagnostics/summary | python3 -m json.tool
curl --fail --silent --show-error http://127.0.0.1:8000/api/v1/remediations/summary | python3 -m json.tool
```

Retrouver les incidents et les executions connues, notamment la correction PHP
validee, puis relancer le playbook : aucun historique ne doit etre recree.
Un endpoint `/health` sain ne suffit pas a prouver la disponibilite du stockage.

Si le redeploiement API echoue apres publication du marqueur, reprendre ce meme
playbook : les journaux migrés et les sources existent encore. Eviter de supprimer
le marqueur ou de revenir a SQLite apres des ecritures PostgreSQL : les historiques
seraient divergents. Un retour demande alors une reconciliation explicite.

## Verification automatique et limites du lot

La CI ajoute un service PostgreSQL 16 ephemere et utilise un compte non superuser.
Les tests couvrent la conservation des valeurs, l'API sans fichiers SQLite,
la reprise des incidents, les IDs, les reservations, les fins concurrentes,
les lectures seules, l'idempotence et l'annulation complete d'une copie en echec.
Ces tests n'injectent aucune panne sur le pilote.

Les comptes et droits utilisateur, la persistance des recommandations de decision,
le catalogue des sauvegardes et les versions des modeles seront ajoutes dans des
lots suivants sur ce meme stockage. La sauvegarde Restic actuelle couvre Moodle ;
elle devra aussi couvrir cette nouvelle base et faire l'objet d'un test de
restauration. Ce lot ne suffit donc pas a declarer le cahier des charges termine.
