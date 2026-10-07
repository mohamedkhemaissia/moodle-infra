# Validation de la remise en service de PHP

Ce scenario est lance par l'operateur sur le pilote Moodle. Il vise a verifier une recuperation reelle, avec les services et l'observateur deja deployes. Il ne constitue pas une boucle de remediation en production.

## Lancement

Depuis le depot `~/moodle-infra` :

```bash
python3 -B -m unittest discover -s scripts/tests -p 'test_validate_php_recovery.py' -v
sudo python3 -B scripts/validate_php_recovery.py
```

Le lancement sans option ne provoque aucune panne. Il verifie les ressources, la collecte, la version de l'API, l'observateur, les conteneurs, la page de connexion HTTPS, les operations en cours et le delai entre actions. Le resultat `ready` signifie que les conditions du scenario sont presentes a cet instant.

Pour executer le scenario de panne et de retour :

```bash
sudo python3 -B scripts/validate_php_recovery.py --run
```

Moodle sera temporairement indisponible. L'operateur doit reserver le pilote a ce test et ne pas lancer de sauvegarde, restauration ou modification de services pendant son execution. Les controles existants ne remplacent pas un verrou partage avec toutes ces operations.

## Deroulement

1. Verifier le pilote. Le CPU doit etre mesurable et inferieur a 80 % au depart, pour garder une marge par rapport au seuil de refus de la remediation (85 %).
2. Armer un minuteur systemd de secours qui demandera le demarrage du meme conteneur PHP apres cinq minutes. Son nom est conserve dans le rapport.
3. Relire les conditions et les identites des conteneurs, puis arreter uniquement le PHP existant avec Docker. Aucun volume ou conteneur n'est supprime.
4. Attendre au plus environ 100 secondes pour qu'un nouvel incident HTTPS soit confirme et qu'une recommandation recente soit utilisable.
5. Appeler la commande existante `moodle-remediate --incident-id N`, qui revalide les conditions avec Ansible Runner.
6. Verifier l'audit de cette execution, sa reservation sur le conteneur attendu et le retour de la page HTTPS et de la sonde Prometheus.
7. Remettre PHP en fonctionnement si necessaire, puis annuler le minuteur de secours apres confirmation de son etat. Une erreur de consultation du secours ne doit pas empecher cette tentative de retour.

## Resultats

- `ready` : controles initiaux satisfaits, sans panne injectee.
- `recovery_validated` : audit de correction valide, retour de Moodle confirme et aucun demarrage par le minuteur de secours observe.
- `blocked` : conditions initiales absentes ; aucun arret confirme n'est provoque.
- `inconclusive` : le scenario a commence mais la preuve de correction est incomplete, l'environnement a interrompu une verification, ou le secours a ete utilise. Ce resultat ne valide pas la remediation.
- `interrupted` : interruption par l'operateur ; une tentative de retour reste effectuee tant que le processus peut executer sa finalisation.

Le rapport JSON est ecrit progressivement dans `ml/data/validation/php_recovery_.../report.json`. Il comprend l'etat initial, les dates du scenario, la sonde pendant la panne, l'identifiant du nouvel incident, l'audit de remediation et le resultat du retour. Ces donnees servent de preuves pour le rapport PFE. La duree indiquee inclut la detection et l'execution de ce scenario ; elle ne constitue pas un RTO general garanti.

La resolution de l'incident reste assuree par l'observateur apres trois controles de retour a la normale. Le resultat du scenario peut donc conserver `incident_resolution: pending`. Verifier ensuite l'episode avec `GET /api/v1/incidents/N`, puis archiver ce resultat avec les autres preuves.

## Limites du secours

Le minuteur depend du fonctionnement du systeme Linux de la VM. Un blocage du noyau peut retarder son execution. Un minuteur transitoire peut aussi disparaitre apres un redemarrage. S'il faut recuperer le pilote manuellement :

```bash
sudo docker start moodle-pilote-php-1
```

Cette commande manuelle ou l'execution du secours ne doit jamais etre presentee comme une recuperation reussie par le module de remediation. Le scenario n'etablit ni une disponibilite generale, ni la qualite de detection des modeles ML, ni une coordination complete avec les sauvegardes et restaurations.

Validation locale du scenario : 12 tests avec doubles de Docker, systemd et de l'API. La correction reelle est a mesurer sur le pilote ; les tests du module de production ont deja execute Runner et le playbook avec leurs interfaces Docker simulees.
