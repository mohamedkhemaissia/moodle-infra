# Politiques de pilotage et périodes d’évaluation

Ce lot met en œuvre le choix du niveau d’autonomie, sa suspension et le contexte d’évaluation prévus par le cahier des charges approuvé. L’installation initialise les politiques en **Observation** et conserve les comptes, incidents et journaux existants.

## Modes

| Mode | Comportement |
| --- | --- |
| Observation | Mesures, incidents, recommandations et diagnostics en lecture seule. Aucune correction autorisée. |
| Assisté | L’administrateur lance `sudo moodle-remediate --incident-id N` depuis le serveur. La commande applique la politique actuelle puis les contrôles techniques. |
| Automatique | Un worker systemd examine les recommandations et peut remettre PHP arrêté en service ou appliquer un profil PHP-FPM préparé. L’activation exige une confirmation explicite de l’administrateur. |

Deux corrections sont reconnues : `start_php_stopped` et `apply_php_fpm_profile`.
La seconde exige sa préparation explicite et une politique enregistrée après celle-ci.
Ses conditions, contrôles et limites figurent dans [les profils PHP-FPM](profils-php-fpm.md).

## Administration et évaluation

La page **Politiques de pilotage** affiche le mode, la suspension, la période d’évaluation, le dernier passage du worker et l’historique. Les lecteurs peuvent consulter ; seuls les administrateurs connectés peuvent modifier ou approuver. Les écritures contrôlent la session actuelle, le rôle, l’origine et le jeton CSRF.

Un changement exige la révision affichée et un motif de 15 à 500 caractères. Les écritures concurrentes ne remplacent pas silencieusement le choix d’un autre administrateur. La politique et son événement d’audit sont enregistrés dans la même transaction PostgreSQL.

Lorsque **Une évaluation est en cours** est active, toute correction de ce module exige une approbation humaine pour l’incident concerné. Le serveur recalcule la recommandation ; le navigateur ne fournit ni preuve technique ni date de validité. L’approbation est liée à l’incident, à la révision de la politique, au contexte et à la version du compte administrateur. Elle expire avec les observations, en 90 secondes au maximum, et est consommée une seule fois. Un compte révoqué ou une politique modifiée invalide son utilisation.

En mode assisté, l’approbation seule ne lance pas la commande. En mode automatique, le worker peut ensuite examiner cet incident et tenter la correction si toutes les conditions sont encore satisfaites.

**Suspendre les nouvelles corrections** bloque les prochaines autorisations. Une action déjà autorisée peut terminer sa vérification. Repasser en Observation interdit également les nouvelles corrections.

## Conditions de correction

Le worker utilise le même Runner et les mêmes contrôles que la commande manuelle : incident Moodle confirmé et récent, données exploitables, collecte récente, ressources acceptables, identité des conteneurs, PHP arrêté, Apache actif, PostgreSQL sain, validation TLS, indisponibilité HTTP compatible, absence de restauration et de maintenance, verrou d’exécution et réservation durable.

La politique est relue juste avant l’autorisation. Une réservation durable précède l’appel Docker ; une issue incertaine n’est pas rejouée automatiquement. Le worker effectue au plus une tentative par incident et révision ; une action autorisée ne peut pas être répétée pour ce même incident. Le délai de protection existant entre corrections reste appliqué.

Après démarrage, trois vérifications HTTPS consécutives et une nouvelle observation Prometheus doivent confirmer le rétablissement. Un score Isolation Forest ou un simple dépassement CPU ne déclenche pas cette correction.

## Persistance et audit

Le schéma PostgreSQL `pilotage_policy` contient la politique, les changements, les approbations, les autorisations, les tentatives du worker et son dernier état. Les autorisations conservent la décision source et la révision/empreinte de la politique. Les rapports de remédiation conservent le mode effectif et les preuves avant/après.

La sauvegarde PostgreSQL du pilotage inclut les sept nouvelles tables lorsque ce schéma est présent. Les anciens profils de sauvegarde restent lisibles. Ce lot ne relance pas le test de restauration complète Moodle reporté.

## Déploiement et contrôle sans incident provoqué

```bash
ansible-playbook ansible/playbooks/policies.yml -K
```

Dans le tableau de bord, vérifier **Observation** après la première installation. Tester le changement vers Assisté avec un motif, activer le contexte d’évaluation, puis suspendre les corrections. Recharger la page pour confirmer la persistance. Un lecteur doit voir ces valeurs et aucun formulaire de modification. Aucun arrêt de PHP n’est nécessaire pour ce contrôle.

Le timer `moodle-remediation-worker.timer` est installé ; en Observation ou lorsque la politique est suspendue, ses passages ne lancent aucune correction. Les erreurs de stockage ou les données inutilisables bloquent l’exécution.

## Validation et limites

Les suites locales vérifient les modes, suspensions, approbations, expiration, révocation, audit, droits API, proxy, rendu des formulaires, Runner et compatibilité de l’inventaire de sauvegarde. Les fixtures SQL locales ne prouvent pas les verrous PostgreSQL.

La suite `tests/postgresql/test_operating_policy.py` utilise le service PostgreSQL isolé déjà présent en CI pour vérifier les transactions et accès concurrents ; aucun nouveau téléchargement Docker n’est ajouté par ce lot. Le résultat de cette suite et la validation d’une correction automatique réelle doivent être consignés après exécution. Un test en mode assisté déjà réussi ne constitue pas une validation du mode automatique.
