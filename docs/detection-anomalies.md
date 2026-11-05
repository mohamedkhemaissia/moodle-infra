# Détection expérimentale des anomalies Moodle

Cette étape entraîne le seul modèle IA retenu dans le périmètre final proposé : Isolation Forest. Il analyse les 21 variables temporelles du CPU, de la mémoire et du disque. Le modèle et son seuil sont appris sur la partition train. Les partitions validation et test servent à observer les résultats.

Le script lit `features.csv` et `metadata.json` produits par `ml.prepare_features`. Il vérifie leurs empreintes, leur chronologie, leurs segments et la séparation des contextes temporels. Il utilise uniquement les variables passées. Les éventuelles tables de cibles futures produites par l'étape historique de préparation ne sont ni entraînées, ni déployées, ni exposées par l'application finale.

## Lancer l'expérience

Depuis la racine du dépôt, avec l'environnement ML existant :

    ~/.venvs/moodle-ml/bin/python -B -m unittest discover \
      -s ml/tests -p 'test_train_anomalies.py' -v

    ~/.venvs/moodle-ml/bin/python -B -m ml.train_anomalies \
      --features-dir ml/data/features/moodle_features_20261006T192230Z_a4188acb4149

Si un nouveau jeu de variables a été préparé, remplacer uniquement le chemin de features-dir. Les résultats sont écrits dans un nouveau sous-dossier de ml/data/models. Une exécution identique réutilise un résultat vérifié. Un résultat existant corrompu provoque un arrêt.

## Résultats

- model.json contient les arbres numériques, l'ordre des variables et le seuil.
- scores.csv contient un score par observation et les deux indicateurs décrits ci-dessous.
- report.json conserve les paramètres, les versions des bibliothèques, les empreintes et les bilans par partition.

Le score élevé indique une observation atypique par rapport au train. Ce score est l'opposé de score_samples de Scikit-learn ; il ne représente pas une probabilité de panne.

Le seuil est le quantile 98 % des scores du train, avec un dépassement strict. Il sert de réglage expérimental et permet d'examiner les observations les plus inhabituelles. Les égalités ne déclenchent pas de signalement ; des variables constantes ne sont pas forcées à devenir des anomalies.

statistical_anomaly indique un dépassement du seuil du modèle. operational_rule_alert indique séparément un CPU au moins égal à 85 %, une mémoire au moins égale à 90 %, ou un disque disponible au plus égal à 10 %. Ces seuils opérationnels sont illustratifs et devront être validés avec BeeCoders.

Un nombre élevé d'anomalies en test peut traduire un changement de charge, un train peu représentatif ou des incidents. Les données complètes ne sont pas présumées normales. Le rapport identifie aussi les variables constantes.

La précision, le rappel et le taux de faux positifs restent à null tant qu'aucune annotation indépendante des incidents n'est fournie. Le taux de signalement du modèle ne remplace pas ces mesures.

## Intégration

Le module isolation_model utilise uniquement la bibliothèque standard Python. Ses arbres JSON sont comparés aux scores du modèle Scikit-learn pendant l'entraînement. Ce format permettra de réutiliser le modèle dans le backend.

Cette version fonctionne en observation. La qualification d'un incident, la politique de décision et les vérifications du service seront nécessaires pour utiliser les scores dans la boucle de pilotage.

## Références

- [Isolation Forest et scores](https://scikit-learn.org/1.7/modules/generated/sklearn.ensemble.IsolationForest.html)
- [Détection d'observations atypiques](https://scikit-learn.org/1.7/modules/outlier_detection.html)

Les données fictives utilisées par les tests vérifient le fonctionnement du logiciel. Les résultats du PFE seront ceux obtenus sur les données et incidents d'essai documentés.
