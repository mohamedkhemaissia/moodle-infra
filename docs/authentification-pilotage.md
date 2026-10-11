# Comptes et acces au pilotage

Les comptes du tableau de bord sont distincts des comptes Moodle. Ils sont
stockes dans le schema `pilotage_auth` de la base PostgreSQL du pilotage.
Les trois schemas d'historiques et les anciennes bases SQLite sont conserves.

## Fonctions

| Fonction | Administrateur | Lecteur |
|---|---|---|
| Consulter mesures, analyses, incidents, decisions, diagnostics et corrections | Oui | Oui |
| Changer son propre mot de passe | Oui | Oui |
| Creer un compte administrateur ou lecteur | Oui | Non |
| Changer le role, desactiver ou reactiver un autre compte | Oui | Non |
| Consulter le journal des acces et changements de comptes via l'API | Oui | Non |

Le controle des droits se fait dans l'API et dans les transactions PostgreSQL.
La derniere personne disposant d'un compte administrateur actif ne peut pas
etre desactivee ou retrogradee. La creation du premier compte passe par une
commande locale root ; aucun identifiant ni mot de passe par defaut.

Un administrateur ne peut pas desactiver son propre compte ni modifier son
propre role, meme si un autre administrateur est actif. L'API refuse ces
changements avec HTTP 409, avant toute modification ou revocation de session.
L'interface identifie « Votre compte » et desactive les deux controles.
Les changements sans effet restent autorises. La recuperation locale root
et la protection du dernier administrateur actif sont conservees.

Les mots de passe de 15 a 128 caracteres sont haches par Argon2id, avec sel
aleatoire, 19 Mio de memoire, deux passages et un niveau de parallelisme de un.
Les tentatives sont limitees dans PostgreSQL et les hachages simultanes dans
l'API. Les erreurs et journaux ne contiennent pas les mots de passe.

La connexion cree une session aleatoire, dont seule l'empreinte est en base.
Le navigateur recoit un cookie HttpOnly, SameSite=Strict. Il ne stocke aucun
jeton dans localStorage. Les ecritures exigent un jeton CSRF et une origine
autorisee. Une session expire apres huit heures ou trente minutes sans
requete authentifiee. Les lectures periodiques d'un tableau de bord ouvert
comptent comme activite. Une desactivation, un changement de role ou de mot
de passe revoque les anciennes sessions. La deconnexion ferme la session
courante. Le journal garde les acces et modifications de comptes.

## Installation sur le pilote

Apres installation des fichiers, tests et verification de la CI :

```sh
ansible-playbook ansible/playbooks/authentication.yml -K
sudo moodle-pilotage-user add --username hamma --role admin --name "Mohamed Khemaissia"
```

La seconde commande demande deux fois le mot de passe, sans l'afficher.
Ouvrir le tableau de bord sur `http://127.0.0.1:8088` ou
`http://localhost:8088` avec le tunnel SSH deja utilise. Se connecter avec
le compte cree, puis creer un lecteur dans **Comptes et acces**. Verifier
que le lecteur consulte les informations et voit uniquement **Mon compte**.

Le playbook initialise uniquement le nouveau schema, prepare l'identite des
commandes d'exploitation, installe la commande de gestion, puis redeploie
l'API et le tableau de bord. Il ne redemarre pas les services Moodle, ne
rejoue pas la migration et ne supprime aucun historique. Le marqueur
`/etc/moodle-pilotage/auth-active` conserve l'activation pour les prochains
deploiements de l'API. Le second passage du playbook doit etre sans changement.

Recuperation locale d'un mot de passe oublie :

```sh
sudo moodle-pilotage-user list
sudo moodle-pilotage-user password --username hamma
```

## Commandes d'exploitation et API

Le secret `/etc/moodle-pilotage/worker-token` est genere sur le serveur et
lisible uniquement par root. L'API recoit son empreinte. Les commandes
`moodle-diagnostic` et `moodle-remediate` peuvent ainsi relire les donnees
necessaires a leurs controles. Cette identite accepte uniquement les lectures
operationnelles autorisees, depuis loopback. Elle ne donne aucun acces aux
comptes, aux sessions utilisateurs ou aux ecritures HTTP.

Les simples appels `curl` aux mesures ou incidents sans session renvoient
desormais **401**, ce qui est attendu. `/api/v1/health`,
`/api/v1/storage/health` et `/api/v1/auth/status` restent lisibles pour les
verifications de disponibilite, sans historique ni secret.

Les tests de trafic utilisant les mesures de l'API doivent etre executes par
l'operateur avec `sudo python3 -B -m ml.traffic_test --profile full`. La cible
Moodle et les garde-fous de charge restent les memes. Les scripts de
validation utilisent la configuration PostgreSQL et l'identite locale.
Les tests avec fixtures n'utilisent jamais le secret du pilote.

## Acces de production

Le parametrage livre sert a l'acces local au laboratoire. Pour un domaine
d'entreprise expose en HTTPS, configurer dans les variables Ansible :

```yaml
control_plane_auth_origins:
  - https://pilotage.domaine-entreprise.tn
control_plane_auth_secure_cookie: true
```

Le reverse proxy HTTPS et son certificat sont a configurer pour ce domaine.
HTTP hors loopback est refuse pour l'origine des ecritures. Uvicorn ignore
les en-tetes de proxy non verifies. Les identites du service sont refusees
sur une connexion directe provenant d'une autre adresse.

## Validation et limites

Les tests Python verifient les cookies, le CSRF, les origines, les roles,
la non-divulgation des mots de passe et les acces limites des commandes.
Les tests React verifient le client HTTP et le rendu des composants selon
le role. La CI utilise PostgreSQL 16 reel avec un compte sans superuser
pour les comptes, sessions, limitations, concurrence et conservation des
historiques. Les tests PostgreSQL ne sont jamais remplaces par un faux backend.

Ce lot ne transforme pas les diagnostics ou corrections en actions HTTP :
la correction PHP conserve son lancement manuel avec controles. Les modes
assiste et automatique, la seconde famille de corrections et la sauvegarde
de la base de pilotage restent a realiser. La qualite des modeles est encore
experimentale et les interruptions de collecte ne sont pas masquees.

References : [OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html),
[OWASP Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html),
[Argon2-cffi](https://argon2-cffi.readthedocs.io/en/stable/).
