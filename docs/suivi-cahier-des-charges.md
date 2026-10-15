# Suivi du cahier des charges

Ce tableau distingue le cahier initial du périmètre final proposé. Le document
de référence détaillé est [perimetre-final-pfe.md](perimetre-final-pfe.md).

| Exigence initiale ou besoin | État réel | Preuve ou suite |
| --- | --- | --- |
| Moodle reproductible et HTTPS | Réalisé dans Codespaces | Déploiement Ansible, Compose, Moodle HTTP 200 et cron contrôlé |
| Supervision Linux, Moodle, PHP-FPM, PostgreSQL et conteneurs | Réalisée | Dashboard, Prometheus, exporteurs et fraîcheur de collecte |
| Alertes et notifications externes | Réalisées | Alertmanager sain et e-mail de recette reçu |
| Détection d'anomalies | Réalisée avec Isolation Forest | Score, seuil, variables observées et limites affichés |
| Prévisions 5/10/15 minutes | Retirées du périmètre final proposé | Modification à faire valider par les encadrants ; historique conservé dans Git |
| Incidents, décisions et diagnostic guidé | Intégrés | Refaire la recette complète et relever les délais |
| Redémarrage PHP-FPM arrêté | Réalisé et borné | Preuve vidéo existante ; répéter avec chronologie complète |
| Profil PHP-FPM Normal/Renforcé | Intégré, gain non encore validé | Essai sous charge progressive et mesures avant/après requis |
| Sauvegarde/restauration de cours | Réalisée dans le pilote | Vérifier ressource, quiz et devoir dans la copie restaurée |
| Sauvegarde/reprise complète locale | Réalisée localement | Mesurer le RTO ; ne pas présenter comme PRA indépendant |
| PRA sur une autre infrastructure et copie hors site | Reporté | Perspective de déploiement BeeCoders |
| Redis, haute disponibilité et multi-serveurs | Reportés ou retirés | Non nécessaires aux trois scénarios du pilote |
| Gestion fonctionnelle multi-rôles | Retirée du périmètre final proposé | Un administrateur BeeCoders utilise le pilote |
| Déploiement interne BeeCoders | Reporté | Après validation du pilote |

## Recette restante

1. Exécuter les trois scénarios de [demo-restitution-2.md](demo-restitution-2.md)
   avec accord explicite et conserver les preuves datées.
2. Comparer règles classiques et Isolation Forest sur les scénarios annotés ;
   calculer précision, rappel, fausses alertes et délai seulement si les données
   le permettent.
3. Mesurer récupération, p95, taux d'erreurs, effet du profil PHP-FPM et durées
   de sauvegarde/restauration sans fabriquer de résultat.
4. Faire valider formellement par les encadrants le retrait des prévisions et
   reporter cette décision dans le rapport et le cahier des charges.

Les tests automatisés prouvent la cohérence du logiciel, pas les performances
opérationnelles ni la valeur scientifique du modèle. Ces conclusions dépendent
de la recette finale documentée.
