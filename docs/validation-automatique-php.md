# Validation de la correction automatique PHP

Ce scénario réutilise les services du pilote et le worker déployé : Supervisor
dans Codespaces, ou le timer systemd sur la VM de référence.
Il ne lance jamais `moodle-remediate` et ne change aucune politique.
L'ancien scénario manuel reste disponible séparément.

## Contrôle préalable

Depuis `~/moodle-infra` :

```bash
sudo /opt/moodle-diagnostics/venv/bin/python -B scripts/validate_automatic_php_recovery.py
```

Ce contrôle consulte les politiques, le timer, les mesures, les incidents,
les états Docker et HTTPS. Il produit un rapport et n'arrête aucun service.
`ready` confirme les conditions techniques du contrôle ; le champ `scheduler`
indique `supervisor` dans Codespaces et `automatic_ready`
indique si le mode est déjà adapté au scénario.

En cas de `resources_not_ready_for_test`, `resource_check` conserve la date
des données, les trois valeurs mesurées, les limites et les ressources bloquantes.
Le contrôle exige CPU < 80 %, RAM < 90 % et disque disponible > 10 % avant
de provoquer une interruption. Ces limites ne sont pas modifiées par ce rapport.

## Scénario sur le pilote, hors période d'évaluation

Le scénario provoque une indisponibilité temporaire de Moodle. L'administrateur
choisit **Automatique**, désactive la suspension et confirme qu'aucune période
d'évaluation n'est active. Un motif peut être :
« Validation contrôlée du rétablissement automatique de PHP sur le pilote ».
Attendre un passage récent du worker puis exécuter le contrôle préalable.

```bash
sudo /opt/moodle-diagnostics/venv/bin/python -B scripts/validate_automatic_php_recovery.py --run
```

Le scénario arme un retour de secours dans cinq minutes, revalide les identités
et la politique, puis arrête uniquement le conteneur PHP du pilote. Il observe
l'incident et l'audit produits par le worker périodique. Le script n'appelle ni
le Runner ni le service de correction pour déclencher l'action.

Dans Codespaces, ce secours est un petit processus root détaché qui ne sait faire
qu'un `docker start` du conteneur PHP préalablement identifié. Le rapport n'est
concluant que si le worker Supervisor agit avant lui. Sur interruption normale,
le script confirme que PHP fonctionne avant d'arrêter le secours.

`automatic_recovery_validated` exige une autorisation automatique avec la
même révision de politique, la réservation du même conteneur et incident,
un Runner réussi, les preuves de rétablissement et aucun recours au secours.
Le script vérifie également HTTPS et une mesure Prometheus récente après
l'action. Le temps consigné inclut la détection, l'attente du timer et la
confirmation du retour ; ce n'est pas uniquement le temps de démarrage PHP.

Une politique modifiée, des données manquantes, une correction manuelle ou
un secours déclenché empêchent de conclure à une correction automatique.
La fermeture de l'incident n'est pas vérifiée par ce scénario.

À la fin, vérifier le retour de Moodle et remettre **Observation** dans le
tableau de bord. Conserver le rapport JSON, l'incident, le journal de correction
et les événements de politique pour la restitution.

## Limites

Les tests locaux de ce script vérifient l'orchestration avec des simulations.
Ils ne prouvent pas une correction automatique sur le pilote. Seul le rapport
du scénario réellement exécuté apporte cette preuve. Aucun test de restauration
complète Moodle n'est lancé par ce lot.
