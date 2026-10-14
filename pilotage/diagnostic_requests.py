"""File PostgreSQL des diagnostics demandes depuis le tableau de bord.

Le service HTTP ne lance aucune commande. Il ajoute une demande idempotente ;
un worker local la reserve ensuite avant d'executer le diagnostic en lecture
seule deja fourni par :mod:`diagnostics.run`.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import math
import re
import sqlite3
import time
import uuid

from .db import load_config, raw_connect


SCHEMA = "pilotage_diagnostic_requests"
APPLICATION_ID = 0x4D445251
STATUSES = ("queued", "running", "completed", "blocked", "failed", "timeout", "canceled", "interrupted")
TERMINAL = STATUSES[2:]
MAX_RUN_SECONDS = 180
DDL = (
    "CREATE TABLE schema_info(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,application_id INTEGER NOT NULL)",
    """CREATE TABLE requests(
        id TEXT PRIMARY KEY CHECK(id ~ '^[0-9a-f]{32}$'),
        incident_id BIGINT NOT NULL REFERENCES pilotage_incidents.incidents(id),
        actor_id BIGINT NOT NULL REFERENCES pilotage_auth.users(id),
        actor_name TEXT NOT NULL,
        client_key TEXT NOT NULL CHECK(client_key ~ '^[0-9a-f]{32}$'),
        status TEXT NOT NULL CHECK(status IN ('queued','running','completed','blocked','failed','timeout','canceled','interrupted')),
        created_at DOUBLE PRECISION NOT NULL,updated_at DOUBLE PRECISION NOT NULL,
        started_at DOUBLE PRECISION,finished_at DOUBLE PRECISION,
        run_id TEXT UNIQUE CHECK(run_id IS NULL OR run_id ~ '^[0-9a-f]{32}$'),
        result_available BOOLEAN NOT NULL DEFAULT FALSE,
        worker_id TEXT CHECK(worker_id IS NULL OR worker_id ~ '^[0-9a-f]{32}$'),
        claim_token TEXT CHECK(claim_token IS NULL OR claim_token ~ '^[0-9a-f]{32}$'),
        deadline_at DOUBLE PRECISION,reason_code TEXT,
        UNIQUE(actor_id,client_key),
        CHECK(reason_code IS NULL OR reason_code ~ '^[a-z][a-z0-9_]{0,95}$'),
        CHECK((status='queued' AND started_at IS NULL AND finished_at IS NULL AND run_id IS NULL
            AND NOT result_available AND worker_id IS NULL AND claim_token IS NULL AND deadline_at IS NULL)
          OR (status='running' AND started_at IS NOT NULL AND finished_at IS NULL AND run_id IS NOT NULL
            AND NOT result_available AND worker_id IS NOT NULL AND claim_token IS NOT NULL AND deadline_at IS NOT NULL)
          OR (status IN ('completed','blocked','failed','timeout','canceled','interrupted')
            AND started_at IS NOT NULL AND finished_at IS NOT NULL AND run_id IS NOT NULL
            AND worker_id IS NOT NULL AND claim_token IS NULL AND deadline_at IS NOT NULL))
    )""",
    "CREATE UNIQUE INDEX one_active_diagnostic_per_incident ON requests(incident_id) WHERE status IN ('queued','running')",
    "CREATE INDEX diagnostic_requests_recent ON requests(created_at DESC,id)",
)


class DiagnosticRequestStorageError(RuntimeError):
    pass


class DiagnosticRequestConflict(ValueError):
    pass


class DiagnosticRequestNotFound(ValueError):
    pass


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("Identifiant de demande invalide.")
    return value


def incident_identifier(value):
    if type(value) is not int or not 1 <= value <= 9223372036854775807:
        raise ValueError("Incident invalide.")
    return value


def encoded(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def timestamp(now=None):
    value = time.time() if now is None else now
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Date invalide.")
    return float(value)


def public_request(row):
    # ``run_id`` est reserve avant Runner pour l'idempotence interne, mais ne
    # devient public qu'apres confirmation de l'ecriture du journal cible.
    run = row["run_id"] if row["result_available"] else None
    return {
        "id": row["id"],
        "incident_id": row["incident_id"],
        "status": row["status"],
        "requested_by": {"id": row["actor_id"], "username": row["actor_name"]},
        "created_at": encoded(row["created_at"]),
        "updated_at": encoded(row["updated_at"]),
        "started_at": None if row["started_at"] is None else encoded(row["started_at"]),
        "finished_at": None if row["finished_at"] is None else encoded(row["finished_at"]),
        "run_id": run,
        "reason_code": row["reason_code"],
        "result_url": None if run is None else "/api/v1/diagnostics/" + run,
    }


class DiagnosticRequestStore:
    def __init__(self, *, database_config=None):
        try:
            self.config = load_config(database_config)
        except sqlite3.Error:
            raise DiagnosticRequestStorageError("Configuration des demandes de diagnostic indisponible.") from None
        if self.config is None:
            raise DiagnosticRequestStorageError("PostgreSQL est requis pour les demandes de diagnostic.")

    @staticmethod
    def check_schema(connection):
        if connection.execute("SELECT version,application_id FROM schema_info WHERE id=1").fetchone() != (1, APPLICATION_ID):
            raise DiagnosticRequestStorageError("Schema des demandes de diagnostic incompatible.")

    @contextmanager
    def connection(self, *, write=False):
        import psycopg
        from psycopg.rows import dict_row
        try:
            with raw_connect(self.config, read_only=not write) as connection:
                with connection.transaction():
                    if write:
                        connection.execute("SELECT pg_advisory_xact_lock(%s,0)", (APPLICATION_ID,))
                    else:
                        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    connection.execute("SET LOCAL search_path TO pilotage_diagnostic_requests,pg_catalog")
                    self.check_schema(connection)
                    with connection.cursor(row_factory=dict_row) as cursor:
                        yield cursor
        except (sqlite3.Error, psycopg.Error):
            raise DiagnosticRequestStorageError("Stockage des demandes de diagnostic indisponible.") from None

    def initialize(self):
        import psycopg
        try:
            with raw_connect(self.config) as connection:
                with connection.transaction():
                    connection.execute("SELECT pg_advisory_xact_lock(%s,0)", (APPLICATION_ID,))
                    exists = connection.execute("SELECT to_regnamespace(%s)", (SCHEMA,)).fetchone()[0]
                    if exists is not None:
                        connection.execute("SET LOCAL search_path TO pilotage_diagnostic_requests,pg_catalog")
                        self.check_schema(connection)
                        return False
                    # Les references garantissent qu'une demande ne vise ni un
                    # incident ni un administrateur inexistant.
                    connection.execute("CREATE SCHEMA pilotage_diagnostic_requests")
                    connection.execute("SET LOCAL search_path TO pilotage_diagnostic_requests,pg_catalog")
                    for statement in DDL:
                        connection.execute(statement)
                    connection.execute("INSERT INTO schema_info VALUES(1,1,%s)", (APPLICATION_ID,))
                    return True
        except DiagnosticRequestStorageError:
            raise
        except (sqlite3.Error, psycopg.Error):
            raise DiagnosticRequestStorageError("Initialisation des demandes de diagnostic indisponible.") from None

    def check(self):
        with self.connection() as cursor:
            cursor.execute("SELECT id,status,run_id,result_available FROM requests LIMIT 0")

    def create(self, principal, incident_id, client_key, *, now=None):
        # Le worker importe ce module sans la pile de mots de passe de l'API.
        # L'autorisation humaine n'est donc chargee que pour une creation HTTP.
        from .recovery import authorize
        incident_identifier(incident_id)
        identifier(client_key)
        now = timestamp(now)
        with self.connection(write=True) as cursor:
            actor = authorize(cursor, principal, now)
            repeated = cursor.execute(
                "SELECT * FROM requests WHERE actor_id=%s AND client_key=%s",
                (actor["id"], client_key),
            ).fetchone()
            if repeated is not None:
                if repeated["incident_id"] != incident_id:
                    raise DiagnosticRequestConflict("Cette cle a deja servi pour un autre incident.")
                return public_request(repeated), False
            incident = cursor.execute(
                "SELECT status FROM pilotage_incidents.incidents WHERE id=%s FOR SHARE", (incident_id,)
            ).fetchone()
            if incident is None:
                raise DiagnosticRequestNotFound("Incident inconnu.")
            if incident["status"] != "open":
                raise DiagnosticRequestConflict("Seul un incident ouvert peut etre diagnostique.")
            active = cursor.execute(
                "SELECT * FROM requests WHERE incident_id=%s AND status IN ('queued','running') ORDER BY created_at,id LIMIT 1",
                (incident_id,),
            ).fetchone()
            if active is not None:
                return public_request(active), False
            row = cursor.execute(
                """INSERT INTO requests(id,incident_id,actor_id,actor_name,client_key,status,created_at,updated_at)
                   VALUES(%s,%s,%s,%s,%s,'queued',%s,%s) RETURNING *""",
                (uuid.uuid4().hex, incident_id, actor["id"], actor["username"], client_key, now, now),
            ).fetchone()
            return public_request(row), True

    def listing(self, *, incident_id=None, limit=10, offset=0):
        if incident_id is not None:
            incident_identifier(incident_id)
        if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError("Pagination invalide.")
        with self.connection() as cursor:
            where, arguments = ("", ()) if incident_id is None else (" WHERE incident_id=%s", (incident_id,))
            total = cursor.execute("SELECT count(*) AS total FROM requests" + where, arguments).fetchone()["total"]
            rows = cursor.execute(
                "SELECT * FROM requests" + where + " ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s",
                (*arguments, limit, offset),
            ).fetchall()
            return {
                "items": [public_request(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(rows) < total,
            }

    def detail(self, request_id):
        identifier(request_id)
        with self.connection() as cursor:
            row = cursor.execute("SELECT * FROM requests WHERE id=%s", (request_id,)).fetchone()
            if row is None:
                raise DiagnosticRequestNotFound("Demande de diagnostic inconnue.")
            return public_request(row)

    def claim(self, worker_id, *, now=None):
        """Reserver au plus une demande ; ne contient aucune execution externe."""
        identifier(worker_id)
        now = timestamp(now)
        with self.connection(write=True) as cursor:
            # Une fin brutale ne laisse pas l'interface bloquee indefiniment.
            cursor.execute(
                """UPDATE requests SET status='interrupted',updated_at=%s,finished_at=%s,
                   claim_token=NULL,reason_code='worker_deadline_exceeded'
                   WHERE status='running' AND deadline_at<=%s""",
                (now, now, now),
            )
            if cursor.execute("SELECT id FROM requests WHERE status='running' LIMIT 1").fetchone() is not None:
                return None
            row = cursor.execute(
                "SELECT * FROM requests WHERE status='queued' ORDER BY created_at,id LIMIT 1 FOR UPDATE"
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            updated = cursor.execute(
                """UPDATE requests SET status='running',updated_at=%s,started_at=%s,run_id=id,
                   worker_id=%s,claim_token=%s,deadline_at=%s WHERE id=%s RETURNING *""",
                (now, now, worker_id, token, now + MAX_RUN_SECONDS, row["id"]),
            ).fetchone()
            return {
                "id": updated["id"],
                "incident_id": updated["incident_id"],
                "run_id": updated["run_id"],
                "claim_token": token,
                "deadline_at": updated["deadline_at"],
            }

    def finish(self, request_id, claim_token, status, *, reason_code=None, result_available=False, now=None):
        identifier(request_id)
        identifier(claim_token)
        if status not in TERMINAL:
            raise ValueError("Etat final du diagnostic invalide.")
        if reason_code is not None and (not isinstance(reason_code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", reason_code)):
            raise ValueError("Motif du diagnostic invalide.")
        if type(result_available) is not bool:
            raise ValueError("Disponibilite du resultat invalide.")
        if status == "completed" and not result_available:
            raise ValueError("Un diagnostic termine exige un audit disponible.")
        if not result_available and reason_code is None:
            raise ValueError("Un resultat indisponible exige un motif borne.")
        now = timestamp(now)
        with self.connection(write=True) as cursor:
            row = cursor.execute("SELECT * FROM requests WHERE id=%s FOR UPDATE", (request_id,)).fetchone()
            if row is None:
                raise DiagnosticRequestNotFound("Demande de diagnostic inconnue.")
            if row["status"] != "running" or row["claim_token"] != claim_token:
                raise DiagnosticRequestConflict("La demande a deja un resultat ou appartient a un autre worker.")
            finished = max(now, row["started_at"])
            if now > row["deadline_at"]:
                status, reason_code = "interrupted", "worker_deadline_exceeded"
            updated = cursor.execute(
                """UPDATE requests SET status=%s,updated_at=%s,finished_at=%s,claim_token=NULL,
                   reason_code=%s,result_available=%s WHERE id=%s RETURNING *""",
                (status, finished, finished, reason_code, result_available, request_id),
            ).fetchone()
            return public_request(updated)

    def requeue(self, request_id, claim_token, *, now=None):
        """Rendre une demande a la file si le verrou Runner est momentanement occupe."""
        identifier(request_id)
        identifier(claim_token)
        now = timestamp(now)
        with self.connection(write=True) as cursor:
            row = cursor.execute("SELECT * FROM requests WHERE id=%s FOR UPDATE", (request_id,)).fetchone()
            if row is None:
                raise DiagnosticRequestNotFound("Demande de diagnostic inconnue.")
            if row["status"] != "running" or row["claim_token"] != claim_token:
                raise DiagnosticRequestConflict("La demande a deja un resultat ou appartient a un autre worker.")
            updated = cursor.execute(
                """UPDATE requests SET status='queued',updated_at=%s,started_at=NULL,finished_at=NULL,
                   run_id=NULL,result_available=FALSE,worker_id=NULL,claim_token=NULL,deadline_at=NULL,reason_code=NULL
                   WHERE id=%s RETURNING *""",
                (max(now, row["updated_at"]), request_id),
            ).fetchone()
            return public_request(updated)
