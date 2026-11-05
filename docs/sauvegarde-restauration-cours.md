# Sauvegarde et restauration de cours depuis le pilote

Le pilote Codespaces propose une fonction volontairement bornee : un
administrateur sauvegarde un cours Moodle en fichier `.mbz`, puis peut restaurer
ce fichier **comme nouveau cours**. Le cours source n'est jamais remplace ni
fusionne par ce parcours.

## Parcours de demonstration

1. Ouvrir **Sauvegardes** dans le tableau de bord.
2. Choisir le cours de demonstration.
3. Cliquer sur **Creer le fichier .mbz** et attendre l'etat `Terminee`.
4. Telecharger l'archive et montrer sa taille et son controle SHA-256.
5. Selectionner cette sauvegarde et une categorie Moodle.
6. Cliquer sur **Restaurer comme nouveau cours**.
7. Relever le nom et l'identifiant du nouveau cours dans l'historique.
8. Ouvrir Moodle et verifier les activites, fichiers, inscriptions et notes.

## Encadrement technique

- Les ecritures HTTP exigent une session administrateur et le jeton CSRF.
- React n'execute aucune commande : l'API depose une demande strictement typee.
- Un conteneur prive, sans port publie et sans socket Docker, appelle les scripts
  officiels `admin/cli/backup.php` et `admin/cli/restore_backup.php` de Moodle.
- Les identifiants, cours, categories et chemins sont valides ; aucune commande
  libre n'est acceptee.
- Le fichier est controle par taille et SHA-256 avant la restauration.
- Une restauration vise toujours une nouvelle copie du cours.

Ce parcours demontre une sauvegarde fonctionnelle de cours. Il ne remplace pas
la sauvegarde complete du site, la copie hors site ni le PRA Restic prevus comme
perspective sur la VM BeeCoders.
