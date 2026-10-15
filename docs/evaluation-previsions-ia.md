# Evaluation des previsions IA

## Objectif

Le pipeline ne considere pas Ridge comme utile par principe. Pour chaque
ressource et chaque horizon de 5, 10 et 15 minutes, il compare trois methodes
sur exactement les memes instants :

- **persistance** : conserver la derniere valeur mesuree ;
- **tendance lineaire** : prolonger la pente calculee sur les cinq dernieres
  minutes ;
- **Ridge** : regression regularisee apprise sur les variables historiques.

La tendance lineaire utilise `valeur_courante + pente_historique * horizon`.
La pente est calculee par moindres carres sur une fenetre terminee a l'instant
de prediction. Aucune cible future n'entre dans ce calcul.

## Protocole reproductible

Les donnees sont separees chronologiquement en 70 % entrainement, 15 %
validation et 15 % test. Ridge est ajuste sur l'entrainement. Son parametre et
la methode diffusee sont choisis sur la validation. Le test n'est consulte
qu'une fois pour presenter le resultat final.

La MAE, la RMSE et le biais sont calcules en points de pourcentage. La MAE est
la mesure principale : plus elle est faible, plus les previsions sont proches
des valeurs reellement observees. En cas d'egalite a `1e-9`, le pipeline garde
la methode la plus simple, d'abord la persistance puis la tendance lineaire.

## Preuves produites

Apres :

```bash
bash scripts/codespaces/train-ai.sh --deploy
```

le dossier `ml/data/reports/moodle_ai_evidence_*/` contient :

- `evidence.json`, exploitable pour verifier tous les chiffres ;
- `jury-summary.md`, tableau directement reutilisable dans le rapport PFE.
- `forecast-comparison.csv`, synthese importable dans un tableur.

Pour chaque ressource et chaque horizon, le tableau indique l'effectif de test,
les trois MAE, les gains de Ridge face aux deux references et la methode choisie
sur validation. Un gain negatif est conserve : il montre honnetement que Ridge
fait moins bien que la reference sur cette experience.

## Texte conseille pour le rapport PFE

> Les previsions Ridge ont ete comparees a deux references : la persistance et
> la prolongation lineaire de la tendance recente. Le decoupage des donnees est
> strictement chronologique. L'entrainement est reserve a la premiere periode,
> le choix des modeles a la validation et les performances finales au test. La
> MAE est calculee pour le CPU, la memoire et le disque aux horizons 5, 10 et 15
> minutes. Cette comparaison permet de verifier la valeur ajoutee du modele sans
> supposer qu'une methode complexe est systematiquement meilleure.

Les valeurs chiffrees du rapport doivent etre copiees depuis le resume genere
sur le pilote reel. Elles ne doivent pas etre inventees ni generalisees a une
autre infrastructure.

## Limites a annoncer

- Les mesures proviennent d'un seul pilote et d'une periode courte.
- Les fenetres successives sont temporellement correlees.
- Une bonne MAE ne constitue pas une prediction de panne.
- La detection Isolation Forest signale un comportement inhabituel, pas une
  cause racine ni une probabilite d'incident.
