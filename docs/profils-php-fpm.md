# Profils PHP-FPM préparés

Deuxième famille de corrections du cahier des charges : augmenter la capacité du
pool lorsque sa file d'attente est confirmée et que les ressources le permettent.

| Profil | Maximum de processus |
| --- | ---: |
| Normal | 5 |
| Renforcé | 8 |

Seule la ligne `pm.max_children` diffère. Les profils sont dérivés de la configuration
protégée, vérifiés avec PHP-FPM et enregistrés dans un catalogue privé.

## Installation sans scénario de panne ou de charge

Enregistrer **Observation** dans les politiques avant la première installation :

```bash
ansible-playbook ansible/playbooks/php-fpm-profiles.yml -K
sudo moodle-fpm-profiles --status
```

Le playbook prépare et publie les profils, sans modifier le pool en service ni
redéployer Moodle. Il conserve comptes, modèles, incidents et journaux. Les
vérifications de module utilisent des fixtures sans arrêter PHP et sans nouvelles
images de test. Les essais réels restent pour la recette finale.

Une politique enregistrée **après** la préparation est nécessaire pour appliquer
un profil. Une ancienne activation automatique ne suffit pas. Conserver Observation
jusqu'à la recette. En évaluation, l'approbation est liée à l'incident et à l'action
exacte, expire avec les observations et n'est utilisable qu'une fois.

## Décision et application

Deux observations doivent confirmer une file positive, aucun processus disponible
et des comptages cohérents. Le compteur cumulé « limite atteinte » ne déclenche pas
l'action. Une collecte absente ou ancienne devient inconnue.

L'action `apply_php_fpm_profile` relit la décision et contrôle les collectes Linux,
HTTPS, PHP et PostgreSQL, les identités et empreintes, les services actifs et la base
saine, ainsi que l'absence de maintenance, sauvegarde et restauration en cours.
Elle exige CPU et RAM sous 70 %, disque disponible au-dessus de 15 %, PostgreSQL sous
70 % de sa limite de connexions et un accès Moodle HTTPS valide.

Le budget réserve trois processus supplémentaires estimés par `memory_limit` plus
32 Mio chacun et 512 Mio pour le système. Une limite mémoire explicite doit aussi
couvrir huit processus et 128 Mio de réserve. Cette estimation ne garantit pas la
consommation des extensions PHP : les mesures après changement restent nécessaires.

Le même verrou, Runner, journal PostgreSQL et délai de protection de 300 secondes
sont utilisés pour les deux corrections. La réservation et l'autorisation sont
durables avant l'écriture ; une seule action est autorisée par incident. Un score
Isolation Forest ou une prévision ne suffit pas à lancer un profil.

Le fichier `/opt/moodle/config/php/zz-moodle.conf`, monté individuellement dans PHP,
garde son inode. Le module valide la configuration effective de `www`, confirme le
maître et envoie `USR2` pour un rechargement gracieux. La réussite exige un nouveau
démarrage du pool, une file vide, un processus disponible, trois collectes PHP
distinctes après changement, des ressources acceptables et HTTPS valide.
La boucle d'observation est bornée à 60 secondes, hors délais propres aux commandes.
Ces contrôles ne prouvent pas à eux seuls un gain de latence ni la cause de saturation.

## Retour précédent et limites

En cas d'échec, le module tente de restaurer le Normal, de le valider et de le
recharger. Un retour confirmé exige un nouveau démarrage du pool et trois réponses
HTTPS valides. L'intervention garde `failed`, même si ce retour réussit. Le journal
distingue fichier restauré sur disque et profil confirmé dans le processus.

Si une identité change ou que le retour n'est pas prouvé, `review_required` et une
transaction `pending.json` interdisent une nouvelle application. Conserver le
journal, revenir en Observation et examiner le conteneur, le montage et le fichier.
Ne pas effacer la transaction pour rendre artificiellement l'action disponible.
Une interruption brutale ou `SIGKILL` ne garantit pas le retour automatique.

Le Renforcé confirmé reste enregistré ; aucun profil supérieur n'est choisi. Un
retour intentionnel au Normal est une opération d'administration à planifier.
Un déploiement général du rôle Moodle peut réappliquer le Normal : suspendre les
corrections avant ce déploiement.

Le tableau de bord et `GET /api/v1/php-fpm/profiles` montrent le **dernier résultat
enregistré**, sa date et son journal, sous la session habituelle. Cette lecture ne
remplace pas les mesures actuelles et n'expose pas le catalogue privé.

Les vérifications locales couvrent autorisations, fichiers protégés, inode, refus
de ressources insuffisantes, contrôle et escalade. Les scénarios et répétitions sont
reportés à la recette finale. Aucun gain réel du Renforcé n'est encore annoncé.
La récupération automatique de PHP arrêté du 10/10 en 86,237 s sans secours reste
une preuve distincte ; ses deux répétitions restantes sont également reportées.
