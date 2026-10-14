# Diagnostics guidés depuis le tableau de bord

## Chaîne d’exécution

```text
Administrateur React
        │ POST borné et idempotent
        ▼
FastAPI /api/v1/diagnostic-requests
        │ écrit seulement une demande
        ▼
PostgreSQL pilotage_diagnostic_requests
        │ réservation atomique, un diagnostic à la fois
        ▼
Worker Linux/Supervisor
        │ revalide la recommandation
        ▼
Ansible Runner diagnose.yml (lecture seule)
        │ écrit le journal réel
        ▼
GET /api/v1/diagnostics/{run_id}
```

L’API HTTP n’accepte ni commande, ni playbook, ni arguments libres. Le corps de création contient uniquement `incident_id` et une clé idempotente hexadécimale de 32 caractères. Seul un administrateur authentifié peut créer une demande ; les sessions humaines authentifiées peuvent consulter la liste et le détail.

## États publics

- `queued` : demande enregistrée ;
- `running` : demande réservée, sans lien de résultat tant que le journal n’existe pas ;
- `completed`, `blocked`, `failed`, `timeout`, `canceled`, `interrupted` : états terminaux.

Le `run_id` et `result_url` ne sont publiés qu’après l’écriture réussie du journal de diagnostic. Une panne précoce du worker produit un état terminal avec un `reason_code` borné, jamais un faux lien vers un rapport absent.

Une échéance ferme clôt une réservation abandonnée. Les écritures utilisent un verrou transactionnel PostgreSQL et une contrainte d’unicité empêche deux demandes actives pour le même incident.

## Limites de sécurité

- Le worker ne traite qu’un incident ouvert et une recommandation actuelle.
- Le playbook exécuté est fixé dans le code et collecte uniquement des preuves.
- Le navigateur ne transmet aucune commande et ne choisit aucun fichier.
- Les sorties libres de Docker, Prometheus ou Runner ne sont pas renvoyées telles quelles par l’API.
- Le diagnostic ne prouve pas une causalité et ne déclenche aucune correction.
- Les remédiations conservent leur propre journal et leurs confirmations ; ce flux ne les appelle pas.

## API

```text
POST /api/v1/diagnostic-requests
GET  /api/v1/diagnostic-requests?incident_id=7&limit=10&offset=0
GET  /api/v1/diagnostic-requests/{request_id}
GET  /api/v1/diagnostics/{run_id}
```

Le proxy Nginx refuse les autres verbes et les chemins supplémentaires. La réponse porte `Cache-Control: no-store`.
