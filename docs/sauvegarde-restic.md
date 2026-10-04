# Sauvegarde coherente du pilote Moodle

Le role `operations` prepare une sauvegarde de la base PostgreSQL, des fichiers
de `moodledata`, du code Moodle, des configurations et du depot Ansible local.
Restic chiffre les archives. Rclone assure le transfert vers le serveur SFTP
Windows configure sur `10.0.2.2:22`, avec le compte `pfe_backup` et une cle SSH.
La cle du serveur est verifiee a partir du fichier `sftp_known_hosts` existant.

## Installer les scripts

Depuis `/home/hamma/moodle-infra` :

```bash
ansible-playbook ansible/playbooks/moodle.yml --syntax-check
ansible-playbook ansible/playbooks/moodle.yml --ask-become-pass
```

Le role conserve les versions Restic 0.19.1 et Rclone 1.75.1. Lorsqu une version
manque, il telecharge l archive ou le paquet officiel, verifie l empreinte
SHA-256 figee, puis l installe. Le mot de passe et les cles existants sont
exiges, jamais regeneres. Le depot Windows existant est reutilise.

Les scripts sont installes sous `/usr/local/sbin` avec les droits root 0750 :

- `moodle-pilote-restic` : commande Restic avec les parametres de connexion.
- `moodle-pilote-backup` : preparation, reprise du site, transfert et controle.

## Premiere sauvegarde complete

```bash
sudo /usr/local/sbin/moodle-pilote-backup
```

Cette commande interrompt temporairement l acces web pendant la preparation
locale. Apache et PHP sont relances avant le transfert vers Windows.

Deroulement :

1. Verifier les fichiers necessaires, les services, l espace libre et le depot.
2. Prendre le verrou du cron existant et attendre la fin de son execution.
3. Arreter Apache, puis PHP ; PostgreSQL reste disponible pour l export.
4. Exporter la base `moodle` au format `pg_dump -Fc` et verifier sa table des matieres.
5. Archiver les fichiers et ecrire le manifeste et les empreintes SHA-256.
6. Relancer les conteneurs existants et liberer le verrou du cron.
7. Envoyer les archives via Restic, puis controler la structure du depot.
8. Enregistrer la reference du snapshot uniquement apres reussite du transfert
   et du controle. Nettoyer les copies temporaires.

En cas d echec apres l arret du site, le script nettoie ses copies temporaires
et tente de relancer Apache et PHP. Si la reprise echoue, il retourne une erreur.
Un depot Windows inaccessible est detecte avant l arret de Moodle.

Les archives locales sont preparees dans `/opt/moodle/backups/staging`, root
0700. Les archives de configuration contiennent les secrets necessaires a la
restauration ; elles sont protegees localement et chiffrees lors de l envoi.
Le journal du cron est bloque par le meme fichier que le script de cron actuel.
Lancer les operations de deploiement et de sauvegarde sequentiellement pour
eviter une modification de configuration pendant sa copie.

Contenu du snapshot :

| Fichier | Contenu |
| --- | --- |
| `moodle.pgdump` | Base PostgreSQL, schema et donnees |
| `moodledata.tar` | Fichiers utilisateurs, permissions et proprietaires |
| `code.tar` | Code Moodle et dependances Composer |
| `configuration.tar` | Compose, configurations, TLS, secrets et construction PHP |
| `infrastructure.tar` | Depot local Ansible, documentation et historique Git |
| `manifest.txt` | Date de coherence, versions, images et etat du depot |
| `SHA256SUMS` | Empreintes des archives et du manifeste |

## Verifier le resultat

```bash
sudo /usr/local/sbin/moodle-pilote-restic snapshots --tag moodle-pilote-full
sudo /usr/local/sbin/moodle-pilote-restic check --read-data
sudo cat /opt/moodle/backups/last-success.txt
sudo docker compose -p moodle-pilote -f /opt/moodle/compose.yml ps
```

Le dernier succes est enregistre dans `last-success.txt`. Le controle
`--read-data` relit aussi les donnees chiffrees du depot. Verifier ensuite
l acces Moodle avec le navigateur et les fonctions du cours de validation.

La creation d un snapshot ne suffit pas pour valider une restauration du site.
La suite du travail consiste a restaurer les archives vers un projet Compose
distinct, avec de nouveaux volumes, puis a effectuer les tests du cahier des
charges. Ne pas extraire une archive directement sur les volumes du pilote.

## Planification

La variable `moodle_backup_schedule_enabled` vaut `false` pendant la validation
initiale. Le fichier cron de sauvegarde est donc prepare avec une entree
desactivee. Le cron applicatif Moodle continue de fonctionner chaque minute.

Apres validation de la sauvegarde complete et d une restauration du site :

```bash
ansible-playbook ansible/playbooks/moodle.yml --ask-become-pass -e moodle_backup_schedule_enabled=true
```

La sauvegarde est alors planifiee a 02:00, heure du serveur Ubuntu. Le parametre
d activation doit ensuite etre conserve dans l inventaire ou les variables du
projet pour les applications suivantes. Les journaux utilisent la rotation
existante de `/var/log/moodle/*.log`.

Le nettoyage automatique des anciens snapshots sera configure apres les tests
de restauration. Les objectifs RTO de 30 minutes et RPO de 24 heures doivent
etre mesures ; les essais du fichier de 55 octets ne les valident pas.

## Recuperation du depot

Le mot de passe Restic est conserve dans `/opt/moodle/secrets/restic_password`
et sa copie Windows dans `C:\MoodleRecovery\restic_password`. Cette derniere
est hors du depot chiffre et son acces NTFS est limite. Elle est indispensable
pour ouvrir les sauvegardes si le fichier de la VM est perdu.

Le stockage Windows reste sur le meme PC physique que la VM : ce montage
valide le laboratoire et la reprise logique, avec cette limite materielle.

## Sources techniques

- https://docs.moodle.org/502/en/Site_backup
- https://www.postgresql.org/docs/16/app-pgdump.html
- https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html
- https://restic.readthedocs.io/en/stable/045_working_with_repos.html
- https://rclone.org/sftp/
- https://github.com/PowerShell/Win32-OpenSSH/issues/2263
