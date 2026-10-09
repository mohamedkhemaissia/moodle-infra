# Catalogue des points de reprise

Le tableau de bord rassemble les references Restic des sauvegardes Moodle
et PostgreSQL du pilotage, dans deux onglets de la section Sauvegardes.
Les administrateurs et les lecteurs authentifies peuvent consulter le catalogue.

## Dates et confirmations

Chaque point indique son identifiant complet, son hote d'origine et le debut
de son archivage. L'instant des donnees est renseigne uniquement lorsque le point
correspond au dernier marqueur local de sauvegarde reussie : gel des ecritures
Moodle ou capture coherente PostgreSQL. Pour les points anterieurs, cet instant
reste inconnu. La date Restic n'est pas utilisee pour inventer un RPO.

La presence dans le depot ne prouve ni une restauration ni l'integrite de toutes
les donnees. Les preuves de restauration PostgreSQL restent dans le suivi deja
existant. Ce lot n'execute aucune sauvegarde, retention ou restauration et
n'ajoute aucune action depuis le navigateur.

## Collecte et API

Le service `moodle-backup-catalog.service` appelle seulement le lecteur Restic
existant avec `snapshots --json --no-lock --no-cache`. Il filtre conjointement
les tags et les chemins de staging attendus, puis publie une projection atomique
sans chemins du depot, identifiants de connexion ou secrets. Aucun acces Docker
n'est ajoute. L'API monte la projection publique en lecture seule.

Le timer actualise le catalogue toutes les cinq minutes. La lecture est bornee
a 45 secondes et 8 Mio ; le service a une limite de 65 secondes. Un echec du
depot publie un etat indisponible. Une projection absente, corrompue, future ou
vieille de plus de 15 minutes masque les references precedentes. Un depot vide
est un resultat distinct d'un depot inaccessible. Le dernier point confirme
localement mais absent du depot est signale explicitement.

`GET /api/v1/backups/catalog?scope=moodle&limit=20&offset=0` accepte les scopes
`moodle` et `pilotage`, 1 a 50 points par page, et au maximum les 200 points
les plus recents de chaque scope. Le nombre total est conserve. La commande de
deploiement peut lire cette route avec son jeton local limite ; ce jeton ne
permet aucune action de reprise.

## Deploiement et recette

Prerequis : lecteur Restic du role operations deja installe, sauvegardes
Moodle et suivi du pilotage precedemment deployes.

```bash
ansible-playbook ansible/playbooks/backup-catalog.yml -K
systemctl list-timers moodle-backup-catalog.timer --no-pager
sudo journalctl -u moodle-backup-catalog.service -n 15 --no-pager -o cat
```

Ouvrir Sauvegardes dans le dashboard et verifier les deux onglets. Comparer
le dernier identifiant Moodle a `/opt/moodle/backups/last-success.txt` et
le dernier identifiant du pilotage a la commande `sudo moodle-pilotage-backup status`.
Developper une reference pour voir ses 64 caracteres. Une date de donnees
non enregistree doit rester affichee ainsi, et non etre remplacee par une date
d'archivage. Repeter le playbook pour verifier `changed=0` et `failed=0`.

Les tests locaux couvrent les contrats, la pagination, la separation des
scopes, les permissions, les limites et les erreurs. Le job CI `backup-catalog`
verifie aussi la version Restic figee par le projet, dans un depot temporaire
avec des donnees fictives. Il cree les points de test, lit le JSON reel,
compare les fichiers du depot avant/apres lecture, simule une retention dans
ce seul depot temporaire et verifie le refus avec un mauvais mot de passe.
Le resultat de ce job et la recette VM doivent etre obtenus avant fusion.

Les contrats et le test Restic sont executes en CI sous Python 3.10.12
(version du collecteur de la VM) et 3.12.15. Les dates Restic a une a neuf
decimales sont validees, puis tronquees a la seconde pour la projection publique.
Le fuseau et les changements de date UTC sont preserves ; les dates malformees
restent refusees. Cette compatibilite ne demande pas de changer Python sur la VM.

## Suite du travail

Le catalogue est une premiere etape du pilotage des reprises. La selection
d'un point pour une restauration, l'approbation administrateur, la journalisation
de la demande et les validations PRA completes restent a implementer.
