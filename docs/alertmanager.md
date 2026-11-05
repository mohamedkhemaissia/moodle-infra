# Alertmanager pour le pilote Moodle

Ce lot complete la supervision prevue dans le cahier des charges. Il reprend
les mesures Linux, HTTPS, PHP-FPM, PostgreSQL et les trois conteneurs du pilote.
L'observateur d'incidents, ses journaux et les workers de correction conservent
leur fonctionnement. Redis reste reporte.

Recette confirmee par l'operateur le 11/10/2026 apres merge de la PR #10 :
premier passage Ansible `ok=21 changed=8 failed=0`, second passage
`ok=19 changed=0 failed=0`. Prometheus charge 16 regles, sans erreur et sans
alerte active au moment du controle. L'interface Alertmanager 0.34.1 est
accessible ; le cluster desactive est attendu pour l'instance unique.
Le relais SMTP reste desactive, donc aucune notification email reelle n'est
encore validee. Cette recette ne doit pas etre rejouee comme un nouveau lot.

## Configuration livree

Prometheus charge les regles et les transmet a Alertmanager sur
`127.0.0.1:9093`. L'image Alertmanager 0.34.1 est verrouillee par son empreinte
multiarchitecture. Le service utilise un compte non privilegie, une racine en
lecture seule, 96 Mio et 0,25 CPU au maximum. Le port de cluster est desactive
pour ce pilote a une seule instance. Le volume `alertmanager_data` conserve les
silences et le journal de deduplication lors des redemarrages.

| Signal | Confirmation | Interpretation |
| --- | --- | --- |
| Collecte absente, en echec, incomplete ou ancienne | 1 minute | Service inconnu ; verifier la collecte. |
| Echec HTTPS | 1 minute | Echec de la sonde de connexion, pas une preuve sur tous les parcours Moodle. |
| PHP-FPM ou PostgreSQL inaccessible | 1 minute | L'exporteur recent ne peut pas joindre le service. |
| File PHP-FPM sans processus libre | 2 minutes | File persistante ; aucun changement de profil declenche par cette alerte. |
| CPU >= 85 % ou memoire utilisee >= 90 % | 3 minutes | Pression de ressource de l'hote. |
| Espace libre de la racine <= 10 % | 5 minutes | Verifier occupation, volumes et retention. |
| Conteneur absent, arrete ou duplique | 1 minute | Identite Compose attendue et inventaire recent obligatoires. |

Les jobs optionnels suivent les variables d'activation deja utilisees par le
projet. La collecte doit dater de moins de 60 secondes. L'inventaire Docker doit
dater de moins de 90 secondes, avec une horloge coherente et sans erreur textfile.
Une perte de collecte ne devient ni une saturation ni un arret confirme.

Les alertes sont regroupees par perimetre et type. Les avertissements attendent
30 secondes avant leur premiere notification, puis au moins cinq minutes entre
changements et quatre heures entre repetitions identiques. Les alertes critiques
attendent dix secondes, avec un intervalle d'une minute et une repetition d'une
heure. Une alerte de collecte inhibe les notifications dependantes du meme job
et du meme perimetre. Les autres services restent visibles.
Cette inhibition peut aussi masquer une notification de resolution dependante
pendant la perte de collecte : le service est alors inconnu, pas confirme retabli.

## Deploiement sur la VM

Apres fusion de la PR et depuis le clone du projet sur la VM :

```bash
git pull --ff-only && ansible-playbook ansible/playbooks/alertmanager.yml -K
```

Le playbook installe uniquement la supervision. Il valide Compose, les regles
avec `promtool`, puis le routage avec `amtool` avant de demarrer les services.
Il controle la disponibilite d'Alertmanager, sa decouverte par Prometheus, les
cibles attendues et la sonde HTTPS. Les services pedagogiques ne sont pas
redeployes par ce playbook.

```bash
ansible-playbook ansible/playbooks/alertmanager.yml -K
```

Ce second passage doit etre sans changement inattendu. Conserver le recapitulatif
Ansible et les resultats suivants comme preuves du deploiement reel :

```bash
curl --fail --silent --show-error http://127.0.0.1:9093/-/ready
```

```bash
curl --fail --silent --show-error http://127.0.0.1:9090/api/v1/alertmanagers
```

```bash
curl --fail --silent --show-error http://127.0.0.1:9090/api/v1/rules
```

Depuis PowerShell, ouvrir un tunnel distinct vers Alertmanager :

```powershell
ssh -p 2222 -i "$env:USERPROFILE\.ssh\id_ed25519_moodle_pfe" -o IdentitiesOnly=yes -N -L 9093:127.0.0.1:9093 hamma@127.0.0.1
```

Ouvrir ensuite `http://127.0.0.1:9093` dans le navigateur. La console locale permet
de consulter les groupes et de creer des silences temporaires pendant une
maintenance. Elle reste accessible aux personnes ayant acces a la VM ou au
tunnel ; elle ne doit pas etre publiee directement sur Internet.

## Email optionnel

Dans GitHub Codespaces, utiliser la procédure courte du
[guide du pilote](codespaces-pilot.md#6-configurer-une-notification-e-mail-externe) :

```bash
bash scripts/codespaces/configure-email.sh
bash scripts/codespaces/deploy.sh
bash scripts/codespaces/test-email.sh
```

Le dernier script envoie seulement une alerte de recette explicitement nommée ;
la réception du message constitue la preuve externe à conserver pour le jury.
La procédure VM ci-dessous reste disponible pour un déploiement hors Codespaces.

Le destinataire et le relais ne sont pas imposes par le projet. Par defaut, le
receiver `local` n'a aucune integration externe : les alertes restent visibles
dans la console Alertmanager et aucun email n'est envoye. Ce mode ne constitue
pas une preuve de livraison SMTP.

Pour activer les emails, creer un fichier de variables prive hors du depot,
par exemple `/home/hamma/moodle-alertmanager-settings.yml`, avec les parametres
du relais autorise et le destinataire reel :

```yaml
monitoring_alert_email_enabled: true
monitoring_alert_smtp_smarthost: smtp.example.test:587
monitoring_alert_smtp_from: moodle@example.test
monitoring_alert_smtp_username: moodle@example.test
monitoring_alert_email_to: operator@example.test
```

```bash
chmod 600 /home/hamma/moodle-alertmanager-settings.yml
```

Un relais sans authentification peut omettre le nom d'utilisateur. Le TLS SMTP
reste obligatoire. Pour un relais authentifie, saisir le mot de passe de facon
interactive sur la VM, sans le placer dans l'historique de commandes :

```bash
sudo python3 -c 'import getpass,os; from pathlib import Path; p=Path("/etc/moodle-alertmanager/smtp-password"); p.parent.mkdir(mode=0o700,exist_ok=True); secret=getpass.getpass("Mot de passe SMTP : "); assert secret and len(secret.encode())<=4096; os.umask(0o077); p.write_text(secret); p.chmod(0o600)'
```

Ansible ajuste ce fichier a `root:65534`, mode `0440`, puis le monte en lecture
seule. Le mot de passe n'est ni lu par une tache Ansible, ni inclus dans le YAML,
ni renvoye dans les journaux. Appliquer les variables privees lors des prochains
deploiements du role monitoring :

```bash
ansible-playbook ansible/playbooks/alertmanager.yml -K -e @/home/hamma/moodle-alertmanager-settings.yml
```

L'email de resolution est active. Valider une livraison avec un destinataire de
test autorise pendant la recette. Apres remplacement atomique du fichier de
mot de passe, recreer uniquement le service pour renouveler son montage :

```bash
sudo docker compose --project-directory /opt/moodle-monitoring --project-name moodle-monitoring up -d --no-deps --force-recreate alertmanager
```

## Verification automatisee et limites

La CI execute `promtool check config`, `promtool check rules`, 26 scenarios de
regles et `amtool check-config` avec et sans SMTP fictif. Un test avec Prometheus
et Alertmanager reels verifie la notification de panne, la perte de collecte,
la resolution, le regroupement, la deduplication, l'inhibition limitee au bon
job et la conservation d'un silence apres redemarrage. Le webhook utilise est
une fixture locale ephemere ; aucune notification n'est envoyee a un tiers.
La resolution HTTPS est verifiee lors d'un retablissement avec collecte active.
Lors d'une perte de collecte, le test verifie separement l'alerte de collecte et
le retrait de l'alerte de service dans Prometheus. Les donnees Docker du test
utilisent des volumes temporaires conserves au redemarrage puis supprimes en
fin d'execution ; les journaux sont recuperes avant le nettoyage.

```bash
python -B ci/pull_ci_images.py alertmanager && python -B ci/run_alertmanager_tests.py
```

Sans Docker, le meme test accepte les quatre executables officiels de versions
identiques dans un repertoire :

```bash
python -B ci/run_alertmanager_tests.py --bin-dir /chemin/vers/binaires
```

Ces verifications ne prouvent pas le deploiement sur la VM ni la livraison du
relais SMTP de l'operateur. Une panne de Prometheus empeche ses regles d'etre
evaluees ; une panne d'Alertmanager empeche l'envoi par ce canal. Leurs controles
locaux restent disponibles pour le diagnostic, mais une supervision externe
independante serait necessaire pour garantir une notification dans ces cas.
Les alertes actives et le journal de deduplication ne constituent pas un
historique d'incidents : l'historique durable reste celui du pilotage existant.

References : [Alertmanager](https://prometheus.io/docs/alerting/latest/alertmanager/),
[configuration des notifications](https://prometheus.io/docs/alerting/latest/configuration/),
[tests de regles](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/),
[version 0.34.1](https://github.com/prometheus/alertmanager/releases/tag/v0.34.1).
