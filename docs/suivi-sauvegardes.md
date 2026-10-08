# Suivi des sauvegardes Moodle

Ce lot ajoute une rubrique **Sauvegardes** dans le tableau de bord, accessible
aux administrateurs et aux lecteurs. Il publie le point de reprise enregistre,
son age, le seuil d'anciennete, l'etat de la planification et du traitement.

## Sources et limites

- Le point vient de /opt/moodle/backups/last-success.txt, deja produit par la
  sauvegarde reussie. L'age part de frozen_at_utc, pas de la fin du transfert.
- systemctl show fournit l'etat du service et du timer. Un service jamais
  execute ne constitue pas une reussite.
- Un collecteur root publie uniquement des informations autorisees dans
  /var/lib/moodle-backup-status/status.json, remplace de maniere atomique.
- L'API monte ce repertoire en lecture seule. Aucun secret Restic, mot de passe,
  fichier de configuration SFTP ni socket Docker supplementaire n'est monte.
- Une publication de plus de 150 secondes, des dates incoherentes ou un fichier
  invalide rendent l'etat inconnu. Une ancienne publication ne reste pas verte.
- Une sauvegarde echouee n'efface pas le dernier point reussi ; le dernier
  resultat du traitement est affiche separement.
- Un point recent est un marqueur enregistre. Cette vue ne prouve pas l'integrite
  actuelle du depot ni la reussite d'une restauration.

Le timer de collecte s'execute chaque minute. Il ne demarre ni sauvegarde,
ni retention, ni restauration. Le seuil utilise moodle_backup_max_age_hours
si l'inventaire le definit ; sinon la valeur pilote est 24 heures.
La valeur est reprise dans les unites lors du prochain deploiement Ansible.

## Deploiement

Apres la fusion de l'authentification et sur une branche dediee :

    ansible-playbook ansible/playbooks/backup-status.yml --syntax-check
    ansible-playbook ansible/playbooks/backup-status.yml -K

Ce playbook installe le collecteur et redeploie uniquement le pilotage et le
dashboard. Il ne reconfigure pas les services Moodle, leur sauvegarde existante
ni les bases de donnees.

Verification de la collecte :

    sudo cat /var/lib/moodle-backup-status/status.json | python3 -m json.tool
    systemctl list-timers moodle-backup-status.timer --no-pager

L'endpoint GET /api/v1/backups/summary exige une session utilisateur ou
l'identite locale de lecture deja utilisee par Ansible. Une requete anonyme
est refusee ; aucun endpoint d'execution n'est ajoute.

Ouvrir le dashboard par le tunnel SSH habituel, se connecter, puis choisir
**Sauvegardes**. Rejouer le playbook et conserver le recapitulatif d'idempotence.

## Recette sur le pilote

1. Verifier que la reference et les dates correspondent au marqueur existant.
2. Verifier que les roles administrateur et lecteur voient la rubrique.
3. Confirmer que les timers de sauvegarde existants conservent leurs horaires.
4. Conserver les preuves de l'API et de la seconde execution Ansible.

Les tests automatises couvrent les fichiers absents ou invalides, les dates
futures, la collecte ancienne, les references mal formees, les ecritures atomiques,
les droits HTTP et le changement d'anciennete entre deux lectures.
Ils utilisent des fichiers temporaires et des etats systemd simules.

## Suite du cahier des charges

Le catalogue Restic complet, les politiques modifiables, les historiques de
restauration et la sauvegarde/restauration de la base PostgreSQL du pilotage
restent a realiser dans les lots suivants. Les sauvegardes Moodle et le PRA
existants restent utilisables.
