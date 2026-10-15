# Suivi des conteneurs du pilote

Ce lot ajoute l'inventaire et les ressources des trois services Moodle essentiels :
Apache, PHP-FPM et PostgreSQL du projet `moodle-pilote`, declares dans
`ansible/inventories/group_vars/moodle/container-monitoring.yml`. Les exporteurs,
services du pilotage, anciens
projets de restauration, les conteneurs de test et les commandes Compose ponctuelles
ne font pas partie de cette liste.

## Mesures et interpretation

| Indicateur | Sens et limite |
| --- | --- |
| Etat | Present, absent, demarre, non demarre, en pause ou plusieurs instances. Une collecte inutilisable produit un etat inconnu. |
| CPU | Variation des compteurs Docker ; 100 % correspond a un coeur, et non a toute la VM. Une premiere lecture sans compteur precedent reste inconnue. |
| Memoire utilisee | Usage Docker moins le cache inactif sous Linux, comme `docker stats`. |
| Limite effective | Limite communiquee par Docker ; sans plafond explicite, elle peut correspondre a la RAM de l'hote. Ce n'est pas une reservation. |
| Processus | Nombre de processus courants communique par le cgroup. |
| Healthcheck | Dernier etat rapporte par Docker lorsqu'un healthcheck existe. Son absence ne devient pas un succes. |

Les statistiques d'un service arrete, absent, duplique ou non mesurable restent
inconnues. Les controles HTTPS, PHP-FPM et PostgreSQL conservent leur role : l'etat
Docker seul ne prouve pas qu'un cours, un depot ou un quiz fonctionne.

## Architecture et fraicheur

Le service systemd `moodle-container-metrics.service` utilise Python standard et
le socket Unix local. Son client expose uniquement GET pour la version, la liste
et les statistiques. Les commandes de modification de Docker ne sont pas utilisees.
Le compte root donne toutefois un acces puissant au daemon : le script et les
unites sont proteges en ecriture et leur code doit etre revu.

Le timer passe toutes les trente secondes apres la fin du passage precedent.
Quatre lectures de statistiques au maximum s'executent en parallele, avec un delai
de trois secondes par appel. Le service est limite a 35 secondes et 96 Mio.
Il ecrit atomiquement `/var/lib/moodle-container-monitoring/textfile/containers.prom`.
Node Exporter monte uniquement ce repertoire en lecture seule et publie les mesures
dans le job Prometheus `linux`. Aucun nouveau conteneur ni nouvelle image de collecte
n'est necessaire sur le pilote.

L'identite du conteneur utilise les labels `project` et `compose_service`.
Le label cible `service=moodle-host` du job Linux reste reserve a l'hote.
Un label textfile `service` entrerait en collision : Prometheus le renommerait
en `exported_service`, et les lignes ne seraient plus reconnues par l'API.
Les tests API et le test d'integration reprennent les labels cibles du pilote.

La date d'inventaire et la date reelle des statistiques sont conservees comme
valeurs de metriques. L'API exige une collecte Node Exporter recente (60 secondes)
et des mesures du collecteur de moins de 90 secondes. Un fichier ancien ne devient
donc pas recent a chaque scrape. Une erreur Docker remplace le fichier par l'etat
de collecte en echec, sans recopier les anciennes ressources. Une erreur de parsing
textfile bloque egalement les lectures. Un delai systemd depasse est detecte par
le vieillissement de la derniere collecte.

L'API `GET /api/v1/metrics/containers` et la section **Conteneurs** exposent ce suivi
aux lecteurs et administrateurs authentifies. Le worker local peut lire cette
route exacte. Nginx refuse les methodes d'ecriture et les chemins d'action inventes.
Le navigateur fait vieillir les etats et les mesures entre deux lectures.

Chaque menu du tableau de bord ouvre sa propre vue. Le lien `#reports` affiche
uniquement les rapports, `#containers` uniquement les conteneurs et `#main` les
ressources actuelles de l'hote. Les liens directs, le rechargement et les boutons
precedent/suivant du navigateur conservent la vue. Les composants des autres vues
ne sont pas montes et leurs lectures periodiques sont arretees. L'analyse
d'anomalies, les incidents et les journaux ne demandent que leurs propres endpoints.

## Deploiement sur la VM

```bash
ansible-playbook ansible/playbooks/container-monitoring.yml -K
```

Ce playbook installe le collecteur et le timer, adapte Node Exporter, puis reconstruit
les images identifiees de l'API et du tableau de bord. Ces deux interfaces peuvent
etre brievement indisponibles. Le role Moodle n'est pas appele ; les services
pedagogiques, profils PHP, politiques, sauvegardes et modeles ne sont pas modifies.
Les etats et erreurs du suivi ne declenchent aucune remediation.

Verifier les trois lignes dans **Conteneurs**, conserver le recap et refaire le
playbook pour verifier l'absence de changements inattendus. Les commandes suivantes
sont des lectures, chacune sur une seule ligne :

```bash
sudo systemctl status moodle-container-metrics.timer moodle-container-metrics.service --no-pager
sudo journalctl -u moodle-container-metrics.service -n 15 --no-pager
curl -fsS http://127.0.0.1:9100/metrics | sed -n '/^moodle_container_/p'
```

Les controles Ansible exigent des etats interpretes dans l'API et le proxy ; un
collecteur actif avec toutes les lignes inconnues ne valide plus le deploiement.

## Verification automatisee et preuves

Les tests couvrent les mesures nulles et non finies, le premier echantillon CPU,
la collecte interrompue, les dates anciennes, les doublons, les droits et le rendu
de l'interface. Le nouveau test CI `ci/run_container_metrics_tests.py` raccorde
Docker reel, le fichier atomique, Node Exporter, Prometheus et la vraie API sur des
conteneurs ephemeres. Il compare un service demarre, un service cree mais non
demarre et un service absent, puis verifie qu'une erreur de collecte masque les
anciennes ressources. Il n'utilise aucun projet du pilote.

Ces tests fonctionnels ne constituent pas une mesure de performance du service
BeeCoders. L'utilisation CPU et RAM du collecteur sera relevee pendant la recette.
Le deploiement VM et la CI doivent etre confirmes separement dans le suivi.
Redis, Alertmanager, les campagnes de charge et le PRA Moodle complet restent a
realiser dans leurs lots propres.

References techniques :
- https://github.com/prometheus/node_exporter#textfile-collector
- https://docs.docker.com/reference/cli/docker/container/stats/
- https://docs.docker.com/reference/api/engine/
- https://prometheus.io/docs/prometheus/latest/configuration/configuration/#scrape_config
