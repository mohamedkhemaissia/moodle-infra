# Tests de restauration approuvés et isolés

Une approbation conserve sa durée de validité de 15 minutes. Elle autorise un démarrage ; elle ne signifie pas qu'une sauvegarde a été restaurée. Dans le dashboard, un administrateur confirme le point complet puis choisit **Lancer le test isolé**. Un lecteur consulte les décisions et les résultats.

Chaque demande possède au plus une exécution. Le worker vérifie de nouveau la session du demandeur, les droits et la version du compte approbateur, la validité de l'approbation et le catalogue récent. Un double clic retrouve la même exécution. Une annulation peut retirer un test en attente ; un test pris en charge ne peut plus être annulé par ce parcours. Une approbation créée avant cette extension exige une nouvelle demande.

## Deux vérifications distinctes

| Sauvegarde | Environnement | Conditions de validation |
|---|---|---|
| Moodle | PostgreSQL, PHP-FPM et Apache temporaires, réseau interne sans port publié, copies de fichiers | SHA-256 des six fichiers, import du dump, accès à la base et aux fichiers, comptages des cours/comptes/quiz, page de connexion HTTP 200 avec formulaire et certificat vérifié |
| Pilotage | PostgreSQL temporaire sans réseau ni port publié | Import du dump, égalité de l'inventaire des tables et de leurs empreintes, conservation des séquences |

Le worker n'arrête aucun service du pilote et ne monte aucun de ses volumes actifs. Il reconstruit son propre Compose et utilise les images locales par identifiant immuable. Les mots de passe copiés ne sont transmis ni à l'API ni au navigateur. Les commandes autorisées passent par un playbook fixe Ansible Runner ; l'API met une demande en attente, sans accès à Docker ou Restic.

La file et les événements d'exécution sont enregistrés dans PostgreSQL. Le profil de sauvegarde du pilotage comprend 21 tables lorsque le journal d'exécution est présent ; les anciens profils de 16 et 19 tables restent lisibles. Un résultat conserve l'identité du point et de la demande, les dates, les vérifications et les preuves de nettoyage.

## Déploiement et première preuve

Après installation des sources, commit/push et CI verte :

```sh
ansible-playbook ansible/playbooks/recovery-tests.yml -K
```

Le playbook initialise seulement les tables additionnelles, conserve les demandes et comptes, puis active un timer de lecture de la file toutes les 15 secondes. Il ne lance aucun test sans demande explicite. Une deuxième application doit obtenir `changed=0` et `failed=0`.

Dans **Sauvegardes** : choisir un point Moodle récent, créer une demande, l'approuver, puis confirmer **Lancer le test isolé** avant l'expiration. Garder la session active jusqu'à la prise en charge. Consulter ensuite le détail et l'historique du test. Refaire le parcours pour un point **PostgreSQL du pilotage**. Seul **Restauration vérifiée** constitue une validation ; un code Runner 0 ne suffit pas.

```sh
sudo systemctl status moodle-recovery-test.timer --no-pager
sudo journalctl -u moodle-recovery-test.service -n 30 --no-pager -o cat
```

Les preuves techniques privées restent sous `/var/lib/moodle-recovery-tests/results`. Les journaux Runner restent dans un dossier privé distinct ; aucune donnée de sauvegarde n'est affichée dans les réponses publiques.

## Délais, interruption et limites

Un seul test est exécuté à la fois. Le budget de commandes par défaut est de 840 secondes, avec arrêt Runner et nettoyage bornés ; le journal refuse une validation après 1080 secondes. La VM doit rester en fonctionnement. Une interruption, une incohérence temporelle ou un nettoyage incomplet produit un résultat non concluant, jamais un faux succès. Après redémarrage ou dépassement du délai, le worker nettoie les ressources portant exactement l'identifiant du test et ne rejoue pas l'action. Tant que ce nettoyage reste incomplet, la file reste bloquée.

Les archives sont limitées à 512 Mio par fichier, sauf `code.tar` qui accepte jusqu'à 1 Gio pour le code Moodle complet. Les fichiers sont téléchargés directement sur disque ; avant chaque transfert, le module exige la réserve libre configurée plus le plafond du fichier concerné. L'extraction reste limitée à 2 Gio par archive, avec au plus 200 000 entrées. Les liens externes, traversées de chemins, périphériques et modifications de configuration arbitraires sont refusés. Les images historiques doivent encore être disponibles localement. Ces limites sont explicites : une sauvegarde dépassant ces plafonds n'est pas déclarée restaurable par ce module.

La CI vérifie les transactions avec PostgreSQL 16, les profils de sauvegarde et les séquences, Runner, l'interface et le backend avec de vrais conteneurs et des fichiers Moodle **fictifs**. Le premier test sur une sauvegarde réelle dans la VM reste nécessaire. Ce parcours démontre la restaurabilité en copie isolée ; il ne mesure pas à lui seul un PRA de bascule en production ni les objectifs RTO/RPO du projet.
