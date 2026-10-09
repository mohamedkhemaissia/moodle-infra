# Suivi des sauvegardes PostgreSQL du pilotage

Ce lot ajoute une zone PostgreSQL dans la page Sauvegardes, a cote du suivi Moodle.
Il lit les preuves produites par `moodle-pilotage-backup` ; il ne lance aucune
sauvegarde, restauration ou correction.

## Donnees affichees

- Dernier point confirme, date de capture, fin, anciennete et seuil configure.
- Planification systemd et resultat du dernier traitement, distincts du dernier point reussi.
- Nombre de tables et d'enregistrements ; controle du depot enregistre lors de la sauvegarde.
- Dernier test de restauration isolee termine, date, duree, comptages et nettoyage.
- Correspondance exacte entre la reference du point teste et celle de la sauvegarde actuelle.

La mention « Point actuel valide » demande un test `restore_validated` sur la
meme reference, la meme date de capture, les memes comptages, des sequences
verifiees et un nettoyage termine. Un test reussi sur un autre point reste une
preuve historique ; il ne valide pas la sauvegarde actuelle. Le dernier test
en echec prime sur une reussite plus ancienne. Ces preuves concernent PostgreSQL
du pilotage ; elles ne remplacent pas un PRA complet Moodle.

## Collecte et acces

Le service existant `moodle-backup-status.service` lit, en tant que root,
`/var/lib/moodle-pilotage-backup/last-success.json` et les rapports `restore-*.json`.
Il publie seulement les champs autorises dans
`/var/lib/moodle-backup-status/pilotage.json` (0644, remplacement atomique).
Il lit les etats systemd avec `show`, sans demarrer les unites concernees.
Les chemins prives, les secrets et les inventaires SQL ne sont pas publies.

L'API expose `GET /api/v1/backups/pilotage/summary`, avec authentification
administrateur/lecteur ou identite locale de lecture, et `Cache-Control: no-store`.
Le conteneur monte uniquement le repertoire public existant en lecture seule.
Les fichiers JSON sont bornes a 8192 octets, sans liens, avec dates UTC
canoniques et rejet des cles dupliquees et nombres non finis. La lecture des
rapports est limitee a 1024 fichiers. Un rapport corrompu rend la validation
inconnue ; il n'est pas ignore au profit d'une ancienne reussite.

La collecte est obsolete apres 150 secondes. Le dashboard masque egalement
une reponse agee de plus de 60 secondes et n'affiche pas un ancien succes comme
un etat actuel. Un echec ulterieur de sauvegarde ne supprime pas le dernier point
reussi ; les deux informations restent distinctes.

## Deploiement

Prerequis : sauvegarde PostgreSQL du pilotage installee, depot au commit
`eb00643` ou sources compatibles, comptes et suivi Moodle deja deployes.

```sh
ansible-playbook ansible/playbooks/pilotage-backup-status.yml -K
```

Ce playbook actualise le collecteur, l'API et le dashboard. Il conserve les
donnees et les configurations privees. Ouvrir la page Sauvegardes apres connexion.
Le playbook verifie la route avec l'identite locale sans afficher son secret.
Il verifie aussi cette route au port du dashboard, a travers Nginx, pour detecter
un oubli dans la liste des routes autorisees par le proxy.
Le relancer doit donner `changed=0` et `failed=0`. Les marqueurs et rapports sont
des preuves locales validees par leur format, pas une attestation independante
de l'etat courant du depot Restic.

## Validation locale

Les tests couvrent la projection sans champs prives, les JSON et fichiers
inexploitables, l'anciennete, l'association au point actuel, un echec plus recent,
les comptages incoherents, les roles et l'absence de route d'execution. Les sources
partagees sont assemblees dans le contexte CI comme lors du deploiement Ansible.
Les donnees du pilote ne sont ni publiees dans Git ni necessaires a ces tests.
