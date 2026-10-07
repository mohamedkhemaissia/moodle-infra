# Premiere remediation : remise en service de PHP arrete

Ce module ajoute une correction reelle au pilote, declenchee par l'operateur avec Ansible Runner. L'action `start_php_stopped` demarre uniquement le conteneur PHP existant du projet `moodle-pilote`. Elle ne recree aucun conteneur ou volume et ne redemarre pas PHP lorsqu'il est deja en fonctionnement.

## Conditions d'execution

La commande `sudo moodle-remediate --incident-id N` exige :

- Une recommandation recente du moteur de decision, issue d'un incident HTTPS Moodle ouvert et confirme. Les autres incidents restent hors de ce premier perimetre.
- Une collecte Prometheus actuelle, avec une sonde Moodle en echec et des mesures CPU, RAM et disque exploitables. Une pression de ressources aux seuils existants suspend la correction.
- PHP dans l'etat `exited`, sans pause, redemarrage en cours ni evenement OOM ; les identifiants et labels Compose doivent correspondre aux services attendus.
- Apache en fonctionnement et PostgreSQL avec son healthcheck `healthy`.
- Une reponse HTTPS 502, 503 ou 504 sur la page de connexion, avec validation du certificat et du nom Moodle. Un certificat invalide, une absence de reponse ou un Moodle deja accessible bloquent l'action.
- L'absence du marqueur de restauration, d'une maintenance hors ligne identifiee dans le volume Moodle et de service systemd Moodle actif de sauvegarde, retention ou restauration.

Ces controles sont relus juste avant l'action. Une reservation SQLite durable est ensuite ecrite avant l'appel `docker start` sur l'identifiant du conteneur verifie. Une seule tentative est permise par incident et deux reservations sont espacees d'au moins cinq minutes. Une action dont l'issue est inconnue n'est pas rejouee automatiquement.

Les gardes ne sont pas une preuve de la cause initiale de l'arret. Ce premier perimetre suppose que l'operateur verifie l'absence d'operation d'administration ou de sauvegarde lancee manuellement. Les controles de maintenance ne constituent pas un verrou partage avec toutes les operations existantes. La coordination complete de ces operations est necessaire avant une activation automatique.

## Verification du retour

Le worker attend au plus 60 secondes pour obtenir :

1. Trois succes consecutifs sur la vraie page de connexion Moodle : certificat et nom verifies, HTTP 200 et champs `username` et `password` presents.
2. Le meme conteneur PHP en fonctionnement, sans etat OOM, pause ou redemarrage en cours.
3. Une sonde Prometheus Moodle reussie, fraiche et collecte apres la reservation de l'action.

Une ancienne valeur `probe_success=1` ne valide pas la correction. Le resultat `completed` exige ces confirmations. Le resultat `failed` peut signifier que PHP a demarre mais que le retour complet n'a pas ete confirme ; les preuves et la reservation permettent alors le diagnostic humain.

L'observateur des incidents reste responsable de la resolution de l'episode, apres ses trois observations de retour a la normale. Le worker de remediation ne modifie jamais directement un incident pour le declarer resolu.

## Audit et isolation

- Code et configuration : `/opt/moodle-remediation`, proprietaire root.
- Environnement Runner reutilise : `/opt/moodle-diagnostics/venv`.
- Journal separe : `/var/lib/moodle-remediation/remediations.sqlite3`, lecture autorisee au groupe de l'API `10001`.
- L'API monte le journal en lecture seule. Elle expose uniquement des GET de consultation ; aucun acces au socket Docker ni route HTTP d'execution n'est ajoute.
- L'audit conserve la decision source, l'action demandee, la reservation, les resultats avant/apres et les evenements Runner normalises. Aucune variable d'environnement Docker, configuration complete ou cle privee n'est exposee.
- Le journal des incidents, celui des diagnostics, les donnees Moodle et les modeles ML restent distincts.

Le mode de cette version est `manual_guarded`. La simulation du moteur de decision reste active et ne constitue pas une autorisation de correction automatique. La commande locale de l'operateur fournit le declenchement explicite. Les modeles ML ne sont pas utilises seuls pour demarrer un service.

## Installation et premier controle

```bash
ansible-playbook ansible/playbooks/remediation.yml -K
sudo moodle-remediate --incident-id 1
curl --fail --silent --show-error \
  'http://127.0.0.1:8000/api/v1/remediations?incident_id=1&limit=5' \
  | python3 -m json.tool
```

L'incident 1 actuel concerne l'historique insuffisant ; `blocked` est donc attendu. Le worker ne doit executer aucune action Docker dans ce cas. La commande renvoie une `detail_url` permettant de verifier la raison dans le rapport.

Routes de consultation : `GET /api/v1/remediations/summary`, `GET /api/v1/remediations?incident_id=N&limit=50&offset=0`, `GET /api/v1/remediations/{run_id}`.

Codes de sortie : 0 pour `completed`, 2 pour un blocage, une execution occupee ou un retour non confirme ; 1 pour une configuration ou un journal indisponible. Une ligne `running` peut subsister apres SIGKILL ou une coupure brutale. La reservation associee indique une intention d'action et ne prouve pas que Docker l'a executee.

## Validation et limites

Les tests couvrent les recommandations perimees, les donnees obsoletes, PHP deja actif, les dependances indisponibles, la maintenance, les echecs TLS, le changement de conteneur, la reservation avant mutation, la prevention des repetitions et la confirmation de nouvelles mesures.

Un test execute reellement Runner et le playbook, avec un serveur HTTPS et une API de test. Docker et systemd sont des doubles de test, et l'IPC interne d'Ansible y utilise TCP local. Ces adaptateurs restent dans des repertoires temporaires et ne sont pas deployes dans le fonctionnement normal du worker. La correction sur les conteneurs reels et le temps de retablissement restent a mesurer sur le pilote.

Une erreur de certificat, un Apache en panne, une base indisponible, PHP actif mais bloque ou une saturation applicative demandent encore un diagnostic. Ce module ne couvre pas ces corrections, le dimensionnement du pool PHP, les restaurations automatiques ou la haute disponibilite. Il apporte le premier cas de remediation effectivement executable, avec une preuve du resultat.
