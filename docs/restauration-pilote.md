# Premier exercice de restauration du pilote

Le playbook `recovery.yml` installe les outils et le controle du cron. Avec
l action `prepare` par defaut, il ne demarre pas la restauration. L import des
donnees est declenche explicitement avec `moodle_recovery_action=restore`.

La commande `restore` recupere le snapshot complet valide depuis Windows dans
un dossier neuf, verifie les fichiers et SHA-256, puis retablit le code et la
configuration. Les images du pilote doivent etre deja presentes sur Ubuntu.
Le projet `moodle-restauration` utilise des reseaux et des volumes distincts.
Ses chemins de montage sont generes a partir du Compose sauvegarde.

| Element | Pilote | Copie restauree |
| --- | --- | --- |
| Projet Compose | moodle-pilote | moodle-restauration |
| Dossier principal | /opt/moodle | /opt/moodle-restauration |
| Volume PostgreSQL | moodle-pilote_postgres_data | moodle-restauration_postgres_data |
| Volume fichiers | moodle-pilote_moodledata | moodle-restauration_moodledata |
| Configuration Compose | /opt/moodle/compose.yml | /opt/moodle-restauration/compose.json |
| URL pendant le test | pilote arrete | https://moodle-pfe.test:8443 |

## Installation

```bash
python3 /home/hamma/Moodle_Restauration_Ansible_20261004/installer.py /home/hamma/moodle-infra
cd /home/hamma/moodle-infra
ansible-playbook ansible/playbooks/recovery.yml --syntax-check
ansible-playbook ansible/playbooks/recovery.yml --ask-become-pass
```

L installateur ajoute le role `recovery`, son playbook et la documentation.
Il ajoute au template de cron existant un controle du fichier
`/opt/moodle/backups/restoration-active`. Le verrou `flock` du cron est conserve.
Ainsi, la suspension persiste pendant l essai et les applications Ansible
suivantes conservent ce controle. Aucun commit ni push n est lance.

## Lancer l exercice avec Ansible

```bash
ansible-playbook ansible/playbooks/recovery.yml --ask-become-pass -e moodle_recovery_action=restore
```

La preparation commence alors que le pilote fonctionne. Avant de lancer les
services de la copie, le script attend la fin du cron, cree le marqueur de
suspension et arrete les trois services du pilote. Leurs volumes sont conserves.
Une seule instance Moodle fonctionne pendant le test.

Ansible declenche le script installe pour creer les ressources de reprise,
restaurer les donnees et verifier le site. La duree technique est enregistree
depuis le lancement de ce script ; l installation initiale des outils est une
precondition preparee dans le meme playbook. Le script reste disponible pour
un diagnostic ou un retour direct : `/usr/local/sbin/moodle-pilote-recovery`.

La base est importee uniquement dans le PostgreSQL de la copie, avec
`pg_restore --single-transaction --exit-on-error --clean --if-exists`.
Le script PostgreSQL sauvegarde recree le role Moodle a l initialisation du
nouveau volume. Les statistiques SQL sont regenerees avec `ANALYZE`.
Les proprietaires numeriques, permissions, ACL et attributs des fichiers
sont retablis. Les dependances Composer sont controlees et les caches Moodle
regenerables sont purges via la CLI avec l utilisateur 33:33.

Le message `RESTAURATION TECHNIQUE OK` exige que l import SQL, la verification
Moodle et la requete HTTPS reussissent. Un echec apres l arret du pilote provoque
une tentative de retour automatique : copie arretee, pilote relance. Un echec
de reprise est signale et le marqueur de suspension est conserve.

Les ressources de copie existantes sont refusees au lancement d un nouvel
import. Le script ne supprime aucun volume. Une importation repetee ou un
nettoyage de copie devra etre prepare explicitement pour les scenarios suivants.

## Validation fonctionnelle

Depuis Windows, ouvrir l adresse habituelle https://moodle-pfe.test:8443.
Ouvrir une nouvelle session et verifier les comptes administrateur et etudiant,
le cours de validation, son PDF, le telechargement et les tentatives/resultats du
quiz. Conserver les preuves avec les dates et les roles utilises.

Le cron automatique du pilote reste suspendu pendant cet exercice temporaire.
Pour verifier le cron de la copie, executer manuellement :

```bash
sudo docker compose -p moodle-restauration -f /opt/moodle-restauration/compose.json exec -T --user 33:33 php php /var/www/moodle/admin/cli/cron.php --keep-alive=0
```

Le fichier `/opt/moodle-restauration/restore-success.txt` indique le snapshot
et la duree technique depuis le lancement du script. Cette duree comprend
la recuperation Restic et la remise en service. Preconditions : Ubuntu pret,
images locales presentes, depot Windows accessible. La fin de la validation
fonctionnelle doit aussi etre horodatee pour la recette de reprise.
Les cibles RTO de 30 minutes et RPO de 24 heures restent des objectifs tant
que la recette correspondante n a pas ete completee.

Le premier exercice etablit la procedure. Les scenarios formels de perte
logique T1, de volumes vides T2 et d echec de sauvegarde T3 seront ensuite
declenches et documentes suivant le cahier des charges.

## Revenir au pilote

Apres les verifications fonctionnelles :

```bash
ansible-playbook ansible/playbooks/recovery.yml --ask-become-pass -e moodle_recovery_action=return
```

Le script arrete la copie, relance le pilote existant, verifie son HTTPS, puis
retire le marqueur pour reactiver son cron. Les volumes et fichiers de la copie
restent presents pour examiner le resultat. Les cours ou fichiers modifies
pendant l essai restent dans la copie : ils ne sont pas reinjectes dans le pilote.
Si le retour echoue, le script tente d arreter le pilote partiellement lance et
de relancer la copie. Le journal indique alors que l operation a echoue.

Commande directe de retour, disponible si Ansible ne peut pas etre lance :

```bash
sudo /usr/local/sbin/moodle-pilote-recovery return
```

Ne pas appliquer le playbook de deploiement `moodle.yml` pendant le test actif :
utiliser la commande `return` pour revenir au pilote avant un deploiement.
La sauvegarde quotidienne reste desactivee pendant cette premiere validation.

## Sources

- https://docs.moodle.org/502/en/Site_restore
- https://www.postgresql.org/docs/16/app-pgrestore.html
- https://docs.docker.com/reference/cli/docker/compose/up/
- https://docs.docker.com/engine/storage/volumes/
- https://moodledev.io/docs/5.2/apis/core/dml

Verification avant livraison : syntaxe Ansible, Jinja stricte et Bash,
installation repetee, separation des montages et simulations d erreurs.
Ces simulations ne remplacent pas la restauration reelle sur la VM.
