# Rapports des incidents et operations

Le menu **Rapports** du tableau de bord permet aux utilisateurs deja autorises
a consulter le pilotage d'exporter les incidents, diagnostics, remediations et
demandes de tests de restauration. Les lectures reutilisent les API et la session
existantes. Aucun nouveau compte, service ou acces de worker n'est ajoute.

## Perimetre d'un export

Selectionner un journal, puis un filtre pour les incidents. Chaque page contient
au maximum 50 entrees. L'export CSV et le rapport HTML reprennent **uniquement
la page affichee**, avec son numero, le filtre, la date de lecture, la date d'export,
la source, le total a la lecture et l'existence d'autres pages. Ils ne constituent
pas un export complet de la base. Le total peut evoluer entre deux lectures si
de nouvelles operations sont enregistrees.

Les identifiants de demande, execution et sauvegarde sont conserves en entier.
Les champs absents restent vides dans le CSV et sont indiques « Non renseigne »
dans le HTML. Une fin de diagnostic ou un retour Runner reussi ne deviennent
pas une preuve de retablissement. Une approbation de restauration ne devient
pas une restauration reussie. Les resultats echoues ou non concluants restent
dans leur etat enregistre.

## Formats

- CSV UTF-8 avec marqueur BOM, separateur point-virgule et dates UTC. Les cellules
  pouvant etre interpretees comme des formules de tableur sont neutralisees.
  Les metadonnees de perimetre sont repetees pour chaque entree. Une page vide
  produit les seuls en-tetes ; choisir le HTML pour conserver ses metadonnees.
- HTML autonome, sans script ni ressource externe, imprimable depuis le
  navigateur. Le contenu des entrees est echappe. Il ne contient que les champs
  publics selectionnes, jamais les objets de configuration, jetons ou secrets.

Le rapport est une capture historique du journal a la date de lecture.
Le bouton **Actualiser le journal** renouvelle cette capture. Une erreur HTTP,
un refus d'acces ou un document incoherent empeche l'export de nouvelles donnees.

## Installation et verification

```sh
ansible-playbook ansible/playbooks/dashboard.yml -K
```

Dans le navigateur, ouvrir **Rapports**, choisir **Incidents**, puis exporter le
CSV et le HTML. Verifier les memes identifiants et etats que dans le tableau.
Le rapport de restauration doit conserver l'etat « Test echoue » pour une demande
dont le test a echoue. Le lecteur peut exporter ce qu'il peut deja consulter.

Les controles automatiques de ce lot concernent les perimetres d'export, les
cellules CSV, l'echappement HTML, les refus HTTP et le rendu React. Ils utilisent
les outils deja presents dans le tableau de bord, sans nouvelle campagne de
restauration ni telechargement d'images de test supplementaires.
