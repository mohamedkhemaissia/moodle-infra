# Supervision PHP-FPM et PostgreSQL Moodle

Ce lot complete les mesures Linux et le controle HTTPS par les indicateurs des
services demandes dans le cahier des charges. Il ne modifie aucun modele ML et
ne declenche aucune correction automatique.

## Indicateurs et interpretation

| Service | Mesures | Portee |
|---|---|---|
| PHP-FPM | Processus actifs, disponibles et totaux ; file d'attente ; limite du pool atteinte | Pool `www`. Le dernier indicateur est un compteur depuis le demarrage, pas un nombre d'incidents ouverts. |
| PostgreSQL Moodle | Connexions de la base et du serveur ; limite du serveur ; transactions validees/annulees par seconde ; blocages mutuels | Transactions moyennees sur deux minutes. Le compteur de blocages peut etre remis a zero avec les statistiques PostgreSQL. |

L'API `/api/v1/metrics/applications` distingue l'etat de collecte Prometheus
(`up`, `down`, `stale`, `missing`) de l'acces au service depuis l'exporteur.
Un service non joignable, une collecte interrompue ou une mesure obsolete ne
devient jamais une valeur zero. L'acces PostgreSQL confirme une connexion de
supervision, et non la reussite d'un quiz ; le controle HTTPS Moodle reste
indispensable. Une nouvelle serie de transactions peut rester inconnue pendant
son amorcage. Les anciennes mesures sont egalement masquees dans l'interface.

## Architecture et acces

- Les exporteurs rejoignent le reseau prive existant pour lire PHP et PostgreSQL,
  ainsi qu'un bridge de supervision non interne pour publier leurs interfaces
  sur la boucle locale de l'hote. Le reseau prive Moodle reste interne.
- PHP utilise un statut FastCGI interne sur le port 9001 ; ce port n'est pas
  publie et aucune route Apache publique n'est ajoutee.
- Les interfaces des exporteurs sont publiees sur `127.0.0.1:9253` et
  `127.0.0.1:9187`. Prometheus les collecte toutes les quinze secondes.
- PostgreSQL utilise `moodle_metrics`, avec `pg_monitor`, sans superuser ni
  autorisation de lecture des tables pedagogiques. Ses transactions sont en
  lecture seule par defaut, avec une limite de cinq connexions et un delai SQL
  de trois secondes. Un compte preexistant incompatible est refuse.
- Le secret est genere sur l'hote et conserve dans
  `/opt/moodle-application-monitoring/secrets/`. Il n'est pas versionne.
- Les exporteurs n'ont ni socket Docker ni volumes de donnees Moodle. Les
  metriques par processus PHP sont exclues des series conservees.
- La nouvelle route est accessible aux lecteurs et administrateurs. L'identite
  locale du worker peut uniquement la lire. Nginx transmet cette route GET.

Versions retenues : `ghcr.io/hackthebox/php-fpm_exporter:2.5.0` et
`quay.io/prometheuscommunity/postgres-exporter:v0.17.1`. Les identifiants d'images
effectivement telecharges doivent etre conserves dans les preuves du pilote.
L'image PHP est figee par le digest multiarchitecture
`sha256:ebb9849b919cc7096ce810b42e82ea73cf719ac6615aad6416ed409a7da80983`.

## Deploiement et recette

```bash
ansible-playbook ansible/playbooks/application-monitoring.yml -K
```

Le premier deploiement telecharge les images avant le changement PHP. La
configuration est validee avec `php-fpm -t`. Seul PHP est redemarre si son statut
vient d'etre active ; les verrous de sauvegarde et de cron sont respectes. Une
restauration declaree active bloque le lot. Si l'application d'un changement
est interrompue, un marqueur conserve la necessite du redemarrage pour la
prochaine execution. PostgreSQL et les volumes pedagogiques ne sont pas
recrees. Le secret et le role ne sont pas regeneres a chaque passage.

L'Ansible deploiement exige les deux exporteurs collectes, puis les indicateurs
disponibles dans l'API et via le vrai proxy du dashboard. Conserver le recap,
une capture de la section **Services Moodle** et un second passage sans
changement. En cas d'echec, conserver la tache et son message plutot que
desactiver ces verifications.

## Validation automatisee

Les suites de l'API et de React couvrent notamment les donnees manquantes,
anciennes, non finies, les valeurs zero, les droits et le passage Nginx.
Les tests de configuration rendent les fichiers reellement deployes.

Le job CI `application-metrics` cree un PostgreSQL et un PHP de test, execute
le SQL du role et rend le vrai Compose des exporteurs. Il verifie leurs ports
publies sur l'hote depuis un Prometheus en mode host, comme sur le pilote, puis
l'idempotence du role, le refus d'acces aux resultats fictifs, la collecte via
l'API, puis la distinction entre arret PHP et arret de son exporteur. Ces
conteneurs sont ephemeres ; ce scenario ne doit pas etre presente comme une
mesure de capacite ou de reprise de l'environnement BeeCoders.

Un environnement sans Docker ne permet pas d'executer ce test d'integration.
Son resultat CI et la recette sur la VM restent des preuves distinctes des
tests locaux de logique et d'interface.

### Correctif reseau v0.1.1

Le retour du deploiement du 09/10 montre quatre cibles existantes collectees et
des connexions refusees sur `127.0.0.1:9253` et `127.0.0.1:9187`.
Le Compose initial raccordait les exporteurs exclusivement au bridge interne
Moodle. La publication peut alors etre acceptee sans creer de liaison effective
sur l'hote avec Docker Engine 29. Le correctif ajoute le bridge de supervision
et exige que le test CI verifie les vraies liaisons de ports du vrai Compose.
Seuls les deux exporteurs rejoignent ce bridge supplementaire ; PostgreSQL ne
recoit ni nouvelle interface reseau ni port publie.

Reference du comportement rapporte :
https://github.com/moby/moby/discussions/53256

References techniques :
- https://github.com/hipages/php-fpm_exporter
- https://github.com/hackthebox/php-fpm_exporter
- https://github.com/hackthebox/php-fpm_exporter/pull/8
- https://github.com/hackthebox/php-fpm_exporter/pkgs/container/php-fpm_exporter
- https://www.php.net/manual/en/install.fpm.configuration.php
- https://github.com/prometheus-community/postgres_exporter

Le suivi des conteneurs, Alertmanager, Redis, les politiques et la seconde
famille de correction restent des lots suivants ; ils ne sont pas declares
termines par ce deploiement.

### Correctif du blocage de collecte v0.1.2

Sur le pilote, `/` de l'exporteur repondait en moins de trois millisecondes
alors que `/metrics` depassait dix secondes sans aucun octet. Les cinq autres
cibles Prometheus restaient collectees. Ce symptome correspond au blocage
documente du client FastCGI de la version hipages 2.2.0 : le delai de connexion
ne bornait pas la lecture. Cela ne determine pas la cause du retard de PHP-FPM.

Le fork Hack The Box 2.5.0 conserve les metriques utilisees et borne l'ensemble
de l'echange FastCGI. Le delai explicite est de trois secondes, inferieur aux
dix secondes du controle Prometheus. L'entree du conteneur est explicite pour
eviter une duplication de la sous-commande `server`. La disponibilite de
l'option est verifiee avec la vraie image avant toute configuration PHP.

Pour un pilote deja installe, appliquer uniquement le remplacement de
l'exporteur, sans relancer PHP, PostgreSQL, l'API ou le dashboard :

```bash
ansible-playbook ansible/playbooks/application-monitoring.yml -K --tags php-exporter-fix
```

Le job CI execute des lectures FastCGI bloquees puis des en-tetes partiels et
exige un retour `phpfpm_up=0` dans le delai. Il verifie ensuite `phpfpm_up=1`
et les compteurs du pool sans redemarrer l'exporteur. Cette regression tourne
sur un conteneur dedie et des donnees fictives, jamais sur le PHP du pilote.
