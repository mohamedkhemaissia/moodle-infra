# Démonstration de la deuxième restitution

## Règles de recette

Les scénarios ci-dessous seront exécutés plus tard avec l'accord de l'opérateur.
Ce document ne demande pas de provoquer maintenant une panne, une charge, une
restauration ou une correction automatique. Avant chaque essai, conserver les
preuves datées de `status.sh`, vérifier les sauvegardes utiles et utiliser le
mode **Assisté**, jamais le mode Automatique.

## Scénario A — Incident PHP-FPM

**Objectif.** Montrer la chaîne détection → incident → diagnostic → décision →
intervention autorisée → vérification → historique.

**Prérequis.** Pilote sain, compte administrateur, collectes récentes, worker de
diagnostic disponible, politique Observation puis Assisté explicitement
autorisée pour l'incident de démonstration.

**Parcours futur.** Relever l'état initial ; provoquer l'arrêt contrôlé de
PHP-FPM ; attendre la confirmation de l'incident Moodle ; demander le diagnostic
guidé ; présenter ses observations ; autoriser uniquement l'action prédéfinie de
remise en service ; vérifier trois sondes HTTPS réussies ; ouvrir l'historique.

**Preuves déjà disponibles.** Déploiement sain, incidents et file de diagnostic
intégrés, worker en lecture seule, intervention PHP-FPM déjà filmée, journaux et
rapports présents.

**Mesures à relever.** Heure de début, délai de détection, durée du diagnostic,
temps de récupération, résultat des contrôles après action et nombre de
tentatives. Répéter l'essai avant de conclure sur la durée moyenne.

**Limites.** Un code retour Runner ne prouve pas seul le rétablissement. La sonde
HTTPS et les observations après action doivent le confirmer. Isolation Forest
peut fournir du contexte, mais ne diagnostique pas la cause.

## Scénario B — Dégradation pendant une activité pédagogique

**Objectif.** Montrer qu'une charge progressive devient observable et qu'une
intervention de capacité reste conditionnelle et administrativement autorisée.

**Prérequis.** Jeu de trafic approuvé, cours de démonstration, métriques PHP-FPM
récentes, profil Normal actif, profil Renforcé préparé, ressources suffisantes
et politique Assisté.

**Parcours futur.** Capturer l'état de référence ; lancer une charge progressive
bornée ; suivre latence, erreurs, processus actifs/disponibles et file PHP-FPM ;
rapprocher les règles et le résultat Isolation Forest ; consulter l'incident et
le diagnostic ; n'autoriser le profil Renforcé que si la file est confirmée et
les garde-fous satisfaits ; comparer les mesures après action.

**Preuves déjà disponibles.** Métriques PHP-FPM affichées, profils Normal (5) et
Renforcé (8) préparés, garde-fous et retour au profil précédent couverts par le
code. Un premier essai `ab` sans erreur a montré une file de 1 et zéro processus
inactif, mais n'a pas validé le bénéfice du profil Renforcé.

**Mesures à relever.** Requêtes/s, p95, taux d'erreurs, file maximale, CPU/RAM,
délai de détection, résultat avant/après et éventuelles fausses alertes. Pour
évaluer Isolation Forest, annoter indépendamment la période et comparer son
signal aux seuils classiques : précision, rappel, fausses alertes et délai ne
sont calculés que si le jeu annoté le permet.

**Limites.** Une anomalie statistique, une hausse CPU ou une file ponctuelle ne
suffisent pas à justifier le changement de profil. Aucun gain n'est affirmé
avant mesures répétées.

## Scénario C — Récupération pédagogique

**Objectif.** Prouver qu'un administrateur récupère un cours utilisable sans
modifier le cours source.

**Prérequis.** Cours de démonstration contenant au moins une ressource, un quiz
et un devoir ; espace disponible ; aucun autre traitement de sauvegarde ou de
restauration en cours.

**Parcours futur.** Ouvrir les sauvegardes de cours ; demander la sauvegarde ;
attendre le fichier `.mbz` confirmé ; demander la restauration comme nouvelle
copie ; ouvrir cette copie dans Moodle ; vérifier le nom, la ressource, le quiz
et le devoir ; conserver le journal et la durée.

**Preuves déjà disponibles.** Sauvegarde et restauration de cours déclarées
fonctionnelles lors de la recette du pilote ; API, worker et écran associés
testés. La sauvegarde et la reprise complète locale existent également, mais ne
sont pas nécessaires à ce scénario pédagogique.

**Mesures à relever.** Durée de sauvegarde, taille du `.mbz`, durée de
restauration, contrôles de contenu réussis/échoués et éventuel nettoyage manuel
après la démonstration.

**Limites.** Restaurer une copie de cours n'est pas un PRA de site. La reprise
complète locale du même Codespace ne prouve ni copie hors site, ni haute
disponibilité, ni reprise sur une infrastructure indépendante.

## Présentation sûre avant les essais

Sans déclencher les scénarios, la restitution peut déjà montrer le statut du
pilote, la supervision, l'écran Isolation Forest, un incident historique, un
diagnostic terminé, les deux profils PHP-FPM, les journaux CSV/HTML, une
sauvegarde de cours existante et l'e-mail de recette. Chaque capture doit être
datée et présentée comme preuve historique, non comme un nouvel essai.
