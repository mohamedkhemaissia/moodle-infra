# Diagnostics manuels avec Ansible Runner

Ce module relie les incidents observes et les recommandations du moteur de decision a des verifications techniques executees par un playbook Ansible. Il conserve leur resultat dans un journal persistant distinct de celui des incidents.

## Fonctionnement

1. L'operateur lance `sudo moodle-diagnostic --incident-id 1`.
2. Le worker relit `/api/v1/decisions/1` et exige une recommandation recente, un incident ouvert et une politique connue.
3. Runner execute le playbook local fixe `diagnostics/project/diagnose.yml`.
4. Le playbook revalide la recommandation au debut effectif des verifications. Il peut accepter une observation plus recente du meme incident, de la meme action, du meme contexte et de la meme politique.
5. Le journal enregistre l'identifiant de l'execution, la decision utilisee, les etats Runner, des evenements normalises et les preuves collectees.
6. L'API permet de consulter ce journal. Elle ne lance aucun playbook.

La politique de decision reste en simulation pour la remediation : `execution.enabled=false` et `runner_configured=false` dans les decisions concernent cette execution future. Le worker de ce module execute uniquement des diagnostics manuels. Les recommandations elles-memes restent calculees a la lecture ; le journal conserve le snapshot effectivement utilise par chaque diagnostic.

## Verifications disponibles

| Profil | Recommandations | Preuves |
|---|---|---|
| Collecte | Inspecter Prometheus ou la collecte Linux/Moodle ; attendre un historique continu | Etat des quatre cibles Prometheus, historique Linux de 420 secondes, etats des conteneurs fixes |
| Ressources | Examiner CPU, RAM, disque ou une observation statistique atypique | Meme collecte et resume actuel des mesures fourni par l'API |
| Moodle | Examiner la disponibilite HTTPS | Collecte, resume de la sonde HTTPS et etat des services |
| Modele | Examiner le modele d'anomalies | Description du modele exposee par l'API |

Ces verifications constituent un diagnostic initial. Elles ne couvrent pas encore les journaux applicatifs, la saturation du pool PHP, les requetes PostgreSQL ni les scenarios de quiz authentifies. Un score statistique ne prouve pas une cause de panne.

L'historique Linux comprend 15 points, espaces de 30 secondes. Un point doit fournir CPU, RAM et disque, avec `up=1` et un age de collecte compris entre 0 et 60 secondes. Une mesure manquante, obsolete ou provenant d'une collecte en echec reste invalide ; elle n'est pas remplacee par zero. Les seuils sont identiques a ceux du pilote actuel.

## Environnement et stockage

- Python 3.12 est fourni par l'interpreteur deja observe dans l'installation uv d'Ansible.
- Un venv independant `/opt/moodle-diagnostics/venv` contient Runner 2.4.3 et Ansible Core 2.21.4, ainsi que leurs dependances verrouillees.
- Le playbook, les sources et la configuration sont copies dans `/opt/moodle-diagnostics`, sous la responsabilite de root.
- Le journal est `/var/lib/moodle-diagnostics/diagnostics.sqlite3`, root proprietaire et groupe `10001` autorise en lecture.
- L'API monte ce repertoire en lecture seule. L'observateur des incidents ne l'utilise pas. Le worker ne modifie pas la base des incidents ni les modeles.
- Les artefacts bruts Runner sont temporaires et prives. Le journal conserve seulement le rapport et les evenements normalises ; aucune configuration Docker complete, variable d'environnement ou cle privee n'est exposee.
- Un verrou interdit les diagnostics simultanes. Runner limite une execution a 120 secondes ; chaque appel HTTP ou Docker a son propre delai de 5 secondes.
- Les appels HTTP sont locaux, sans proxy et sans suivi des redirections. Les conteneurs verifies sont ceux du pilote actuel et sont fixes dans le code.

Pour un autre serveur, adapter `diagnostics_python_source` a un interpreteur Python 3.12 et les noms des conteneurs a la topologie reelle. Il ne s'agit pas d'un inventaire de production universel.

## Installation et consultation

```bash
ansible-playbook ansible/playbooks/diagnostics.yml -K
sudo moodle-diagnostic --incident-id 1
curl --fail --silent --show-error \
  'http://127.0.0.1:8000/api/v1/diagnostics?incident_id=1&limit=5' \
  | python3 -m json.tool
```

Le lancement renvoie un `run_id` et une `detail_url`. La lecture de cette URL retourne les preuves. Les routes sont :

- `GET /api/v1/diagnostics/summary`
- `GET /api/v1/diagnostics?incident_id=1&limit=50&offset=0`
- `GET /api/v1/diagnostics/{run_id}`

`completed` signifie que les verifications ont ete executees et journalisees. Un rapport termine peut contenir des controles `warning` ou `unavailable` ; il ne certifie pas un service sain. `blocked` signifie que la recommandation ne permettait plus le diagnostic. `failed`, `timeout` et `canceled` conservent l'issue de l'execution. Apres une coupure brutale ou SIGKILL, une ligne `running` peut subsister ; elle ne constitue pas une preuve de processus encore actif.

Si l'incident 1 est resolu ou si l'observateur est obsolete, le blocage est attendu. Ne pas contourner la revalidation ni modifier les seuils pour obtenir artificiellement un lancement.

## Validation

Tests du journal, des recommandations perimees, de la concurrence, de l'interruption, des erreurs et des historiques incomplets. Un test execute reellement Runner, Ansible Core et le playbook contre un serveur HTTP local de test ; les sondes Docker y restent sans conteneurs reels. Dans le banc d'essai distant, le transport IPC interne d'Ansible utilise TCP local, car les sockets Unix sont interdites. Cette adaptation du banc d'essai est exclue du paquet ; la VM utilise le transport standard d'Ansible. Le deploiement Docker et les verifications sur les services reels restent a confirmer sur la VM.

L'installation Ansible execute les tests du worker avant de deployer la nouvelle API. Le Dockerfile conserve l'execution des tests de l'API pendant la construction. Une seconde execution du playbook doit etre sans changement.

References techniques : [Ansible Runner](https://docs.ansible.com/projects/runner/en/stable/), [Runner 2.4.3](https://pypi.org/project/ansible-runner/2.4.3/), [Ansible Core 2.21.4](https://pypi.org/project/ansible-core/2.21.4/).
