# Détection des anomalies dans l’API Moodle

L’API finale expose le modèle Isolation Forest en mode observation. Il est le seul modèle IA entraîné et déployé par le parcours principal.

## Deux endpoints

- `GET /api/v1/anomalies/model` décrit le modèle chargé, son identifiant, sa période d’entraînement, son seuil et les comptages des partitions. Il ne consulte pas Prometheus et ne décrit pas la santé actuelle du serveur.
- `GET /api/v1/anomalies/summary` calcule le score des ressources récentes à partir de Prometheus. Il renvoie les mesures courantes, le score, le seuil, un indicateur statistique et les dépassements des seuils opérationnels.

`statistical_anomaly = true` signifie que le score est strictement supérieur au seuil appris sur train. Ce score n’est pas une probabilité de panne. Les seuils opérationnels CPU ≥ 85 %, RAM ≥ 90 % et disque disponible ≤ 10 % proviennent du rapport d’entraînement actuel ; ils restent à valider avec BeeCoders. Les deux types de signalement sont indépendants.

## Qualité des données

Le calcul d'anomalie relit l'historique nécessaire aux 21 variables temporelles. Une fenêtre de cinq minutes, complétée par les deux minutes de contexte nécessaires à la mesure CPU, exige 420 secondes d’historique continu. Les ressources sont arrondies comme dans l’export d’entraînement. Les variables sont calculées uniquement sur la fenêtre passée.

Si la collecte est absente, arrêtée, trop ancienne ou incomplète, l’endpoint renvoie HTTP 200, `status = unavailable`, une raison et `anomaly = null`. Une donnée manquante n’est pas remplacée par zéro. Une mesure CPU réellement égale à zéro reste valide.

Une réponse disponible est mise en cache pour le point courant de la grille. Chaque réutilisation vérifie à nouveau la collecte actuelle. Une erreur de connexion Prometheus renvoie HTTP 503 ; une réponse inexploitable renvoie HTTP 502.

## Artefacts et déploiement

Le service charge `model.json`, `report.json` et une copie des métadonnées des variables. Il vérifie les empreintes, la provenance, l’ordre des variables, le seuil et la structure des arbres. Le modèle est représenté par des données numériques JSON.

Le moteur `ml/isolation_model.py`, déjà utilisé pendant l’entraînement, est copié par Ansible dans l’image sous `app/isolation_model.py`. Il n’existe pas de seconde copie à maintenir dans le dépôt. Le service n’installe ni NumPy ni Scikit-learn pour l’inférence. Les tests de l’API sont exécutés pendant la construction Docker.

Chaque version des artefacts des anomalies possède un répertoire identifié par les empreintes des trois fichiers. Ansible valide cette version avant de modifier la configuration du service actif. Le conteneur monte les artefacts en lecture seule. Le rôle conserve l’accès sur `127.0.0.1:8000` et les limites de ressources existantes.

## Interpréter la validation

Les tests techniques vérifient l’intégration, les calculs et les refus sur des données invalides. Ils ne mesurent pas la précision de détection d’incidents réels. Cette mesure attend des scénarios annotés et des observations plus représentatives.

Ce module ne crée pas encore d’incident persistant et n’exécute pas de remédiation. La gestion des incidents, les politiques d’autorisation et le moteur de remédiation constituent les étapes suivantes.
