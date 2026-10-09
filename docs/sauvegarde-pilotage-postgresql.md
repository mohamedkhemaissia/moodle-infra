# Sauvegarde de la base du pilotage

Ce lot protege les incidents, diagnostics, corrections et comptes du dashboard.
La base Moodle conserve sa chaine de sauvegarde actuelle. Le nouveau point est
envoye au meme depot Restic chiffre, via le wrapper et la connexion SFTP existants,
avec le tag distinct `moodle-pilotage-database`. Il ne modifie aucun mot de passe.

## Coherence et contenu

Une transaction PostgreSQL en lecture seule exporte un instantane. `pg_dump 16`
utilise exactement cet instantane pendant que l'inventaire des quinze tables est
calcule. Les ecritures suivantes de l'observateur et des comptes restent possibles
mais n'entrent pas dans ce point. Aucune interruption de Moodle n'est demandee.

Le point contient le dump au format custom, un inventaire avec comptages et
empreintes des lignes/colonnes, un manifeste SHA-256 et les configurations/cles
necessaires a une future reprise du pilotage. Les secrets existent seulement dans
un dossier root protege puis dans Restic chiffre ; ils ne sont pas affiches dans
les rapports. Les roles globaux de PostgreSQL ne sont pas exportes : le compte
applicatif est reconstitue avec les droits du fichier init.sh conserve.

Le marqueur `last-success.json` est publie apres confirmation du point distant et
`restic check`. L'echec d'un dump ou du transfert preserve le dernier marqueur.
Ce check controle la structure du depot ; la restauration teste les contenus du
point choisi. Le tag distinct evite qu'une retention Moodle ne selectionne ces
points. Une politique de retention propre au pilotage reste a integrer au catalogue.

## Installation et premiere sauvegarde

Prerequis : PostgreSQL et les comptes du pilotage deja actifs ; connexion Restic
du pilote valide ; service SFTP Windows joignable. Aucun secret n'est a recopier.

```bash
ansible-playbook ansible/playbooks/pilotage-backup.yml -K
sudo systemctl start moodle-pilotage-backup.service
sudo moodle-pilotage-backup status
systemctl list-timers moodle-pilotage-backup.timer --no-pager
```

Le timer quotidien est fixe a 03:15 UTC, ajustable par l'inventaire Ansible.
Un verrou commun evite la concurrence avec sauvegarde, conservation et restauration
Moodle. Un conflit bloque cette execution ; il ne remplace pas le point precedent.
Le transfert, les commandes Docker et les requetes SQL ont des delais bornes.

## Test de restauration isole

```bash
sudo moodle-pilotage-backup test-restore
```

Sans argument, le test lit l'identifiant exact du dernier succes ; il n'utilise
jamais un `latest` non filtre. Un point precis peut etre choisi avec
`--snapshot IDENTIFIANT_COMPLET_64_HEXADECIMAUX`.

Les fichiers sont relus depuis Restic et leurs SHA-256 verifies. Un conteneur
PostgreSQL 16 est cree avec l'image exacte du point, un repertoire de donnees neuf,
aucun port et `network=none`. Il utilise un compte applicatif sans droits superuser.
Les quinze tables, leurs colonnes et leurs empreintes sont comparees a l'instantane
sauvegarde, puis les sequences d'identifiants sont verifiees. Les donnees et services
actifs ne sont jamais montes, effaces, arretes ou remplaces.

Les rapports root sont conserves sous `/var/lib/moodle-pilotage-backup/reports`.
Le test supprime uniquement son conteneur portant l'etiquette de son execution et
son dossier temporaire. Si ce nettoyage n'est pas confirme, le resultat reste
`inconclusive` et le dossier prive est conserve pour inspection.

Resultat attendu : `status=restore_validated`, `tables_verified=16`,
`sequence_check=passed`, `production_changed=false` et nettoyage confirme.
Rejouer le playbook doit conserver la configuration : `changed=0`, `failed=0`.

## Preuves et limites

Les tests unitaires utilisent des fixtures : ils prouvent les protections des
commandes et des rapports, pas une restauration du pilote. La CI PostgreSQL ajoute
un vrai cycle `pg_dump/pg_restore` dans deux bases de test uniques, avec une ecriture
apres l'instantane pour verifier l'absence de fuite temporelle. Les deux bases et
le compte CI sont ensuite supprimes. Le test operateur ci-dessus est la preuve
de restauration depuis le depot SFTP reel et doit etre conserve pour le rapport.

La duree mesuree est celle du test technique, pas le RTO complet d'une reprise de
service. Le point PostgreSQL du pilotage et celui de Moodle ont chacun leur instant ;
ils ne constituent pas une transaction commune. Ce lot ne bascule pas l'API sur
une copie restauree et ne prouve pas encore un PRA complet sur un autre hote.
L'interface affiche actuellement la sauvegarde Moodle ; le catalogue centralise et
la reprise applicative du pilotage restent les prochaines etapes.

References techniques :
- https://www.postgresql.org/docs/16/app-pgdump.html
- https://www.postgresql.org/docs/16/functions-admin.html#FUNCTIONS-SNAPSHOT-SYNCHRONIZATION
- https://restic.readthedocs.io/en/stable/050_restore.html
