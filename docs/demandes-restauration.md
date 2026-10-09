# Demandes de test de restauration et approbation

Ce lot ajoute le choix d'un point de reprise dans Sauvegardes, la demande d'un
test en environnement isole et sa decision par un administrateur. Le lecteur
consulte la liste et le detail. Les comptes existants servent au parcours ;
aucun compte ou mot de passe par defaut n'est cree.

Les demandes et evenements sont conserves dans le schema PostgreSQL
`pilotage_recovery`. La selection complete, le motif, le demandeur, les dates,
la revision, la decision et les preuves du catalogue sont enregistres. Une
demande suit les etats en attente, approuvee, refusee ou annulee. Une
approbation est valable 15 minutes ; son expiration n'efface pas l'historique.
Il faut une nouvelle demande apres expiration. L'administrateur peut annuler
une demande en attente ou approuvee, y compris apres cette expiration.

L'approbation ne lance aucune commande. L'API ne propose pas d'execution,
de restauration de production, de suppression de point ni de modification de
la reference d'une demande. Le worker d'execution des tests et le parcours de
reprise de production restent des increments suivants. Avant toute execution
future, ils devront revalider l'autorisation, le point dans le depot, les
verrous, l'environnement cible et la validite de l'approbation.

## Garanties du parcours

- Session active et role administrateur, origine exacte et verification CSRF.
- Reverification de la session dans la transaction metier ; une revocation
  entre le middleware et l'ecriture bloque la mutation.
- Reference Restic de 64 caracteres, perimetre Moodle ou pilotage et motif de
  15 a 500 caracteres. Aucun chemin ou identifiant d'acteur fourni par le client.
- Catalogue valide et recent a la creation et a l'approbation. La presence d'un
  point n'est pas une preuve de restauration. Une date des donnees inconnue
  reste inconnue. Un marqueur avance n'invalide pas une ancienne reference.
- Demande et audit dans la meme transaction. Une revision obsolete bloque une
  decision concurrente ; une relance identique n'ajoute pas un second evenement
  et ne renouvelle pas l'approbation.
- Identite d'exploitation locale exclue de ces routes humaines. Le conteneur
  API garde seulement les montages existants en lecture seule et n'acquiert
  aucun acces Docker ou Restic.

## Deploiement et sauvegardes

`ansible/playbooks/recovery-requests.yml` installe d'abord la compatibilite de
la sauvegarde PostgreSQL, puis initialise le schema sans toucher aux journaux
precedents. Il actualise les collecteurs, l'API et le dashboard. Il ne redeploie
pas les services applicatifs Moodle et ne lance pas de restauration.

La capture PostgreSQL detecte le nouveau schema dans le meme instantane que
les donnees. Le profil existant de 16 tables reste pris en charge. Le nouveau
profil couvre 19 tables et ajoute le controle de la sequence des evenements.
Le format du paquet Restic demeure `moodle-pilotage-backup-v1` ; les empreintes
et l'inventaire de chaque sauvegarde determinent les tables a verifier lors du
test isole. Le marqueur `recovery-requests-active` n'est pas une donnee metier ;
une reconstruction passe par le playbook apres restauration des donnees.

## Recette operateur

1. Executer le playbook ; le second passage doit etre sans changement ni echec.
2. Se connecter comme administrateur, ouvrir Sauvegardes et choisir un point
   recent dans l'un des deux catalogues. Cliquer sur **Demander un test**.
3. Enregistrer un motif ; retrouver la demande en attente et sa reference
   complete. L'instant des donnees peut rester non enregistre.
4. Confirmer la reference et saisir le motif d'approbation. Verifier l'etat
   approuve puis recharger la page : les deux evenements restent presents.
5. Annuler cette demande de demonstration avec un motif. Tester une seconde
   demande refusee. Le detail garde les decisions precedentes.
6. Se connecter comme lecteur : consultation possible, boutons de decision
   absents. Conserver captures et identifiants des demandes dans les preuves.
7. Lancer, au moment choisi, la sauvegarde PostgreSQL puis son test isole par
   les commandes operateur existantes. Le nouveau point doit couvrir 19 tables.

Les tests locaux couvrent les contrats API, droits, formulaires et erreurs de
catalogue, ainsi que l'orchestration de sauvegarde avec fixtures. La CI ajoute
les transactions reelles PostgreSQL 16, concurrence, audit atomique et
`pg_dump`/`pg_restore` des deux profils. Un resultat CI vert et la recette VM
restent necessaires avant la fusion de ce lot.
