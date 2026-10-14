"""File de demandes de diagnostic : aucune execution depuis l API."""
from contextlib import contextmanager
import re
import sqlite3
import uuid
import psycopg
from psycopg.rows import dict_row
from .auth import timestamp
from .db import load_config, raw_connect
from .recovery import authorize, note

class RequestConflict(ValueError):
    pass

class RequestStorageError(RuntimeError):
    pass

class DiagnosticRequests:
    def __init__(self, database_config=None):
        try:
            self.config = load_config(database_config)
        except sqlite3.Error:
            raise RequestStorageError("Configuration PostgreSQL indisponible.") from None
        if self.config is None:
            raise RequestStorageError("PostgreSQL requis.")

    @contextmanager
    def connection(self, write=False):
        try:
            with raw_connect(self.config, read_only=not write) as db:
                with db.transaction():
                    if write:
                        db.execute("SELECT pg_advisory_xact_lock(%s,%s)", (0x4D444941, 7))
                    with db.cursor(row_factory=dict_row) as cursor:
                        yield cursor
        except (sqlite3.Error, psycopg.Error):
            raise RequestStorageError("File des diagnostics indisponible.") from None

    def create(self, principal, *, incident_id, reason, client_key):
        if type(incident_id) is not int or not 1 <= incident_id <= 9223372036854775807:
            raise ValueError("Incident invalide.")
        if not isinstance(client_key, str) or re.fullmatch("[0-9a-f]{32}", client_key) is None:
            raise ValueError("Reference de demande invalide.")
        reason, now = note(reason), timestamp()
        with self.connection(write=True) as cursor:
            actor = authorize(cursor, principal, now)
            old = cursor.execute("SELECT * FROM pilotage_diagnostics.requests WHERE actor_id=%s AND client_key=%s", (actor["id"], client_key)).fetchone()
            if old is not None:
                if (old["incident_id"], old["reason"]) != (incident_id, reason):
                    raise RequestConflict("Reference utilisee pour une autre demande.")
                return {"id": old["id"], "status": old["status"], "incident_id": incident_id}, False
            incident = cursor.execute("SELECT id FROM pilotage_incidents.incidents WHERE id=%s AND status=%s", (incident_id, "open")).fetchone()
            if incident is None:
                raise RequestConflict("Incident non ouvert ou inconnu.")
            if cursor.execute("SELECT 1 FROM pilotage_diagnostics.requests WHERE incident_id=%s AND status IN (%s,%s)", (incident_id, "pending", "running")).fetchone():
                raise RequestConflict("Diagnostic deja demande pour cet incident.")
            identifier = uuid.uuid4().hex
            cursor.execute("INSERT INTO pilotage_diagnostics.requests(id,incident_id,actor_id,client_key,reason,status,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)", (identifier, incident_id, actor["id"], client_key, reason, "pending", now))
            return {"id": identifier, "status": "pending", "incident_id": incident_id}, True

    def listing(self, *, incident_id=None, status=None, limit=10, offset=0):
        if incident_id is not None and (type(incident_id) is not int or not 1 <= incident_id <= 9223372036854775807):
            raise ValueError("Incident invalide.")
        if status is not None and status not in ("pending", "running", "completed", "blocked", "failed", "interrupted"):
            raise ValueError("Statut invalide.")
        if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError("Pagination invalide.")
        conditions, params = [], []
        if incident_id is not None:
            conditions.append("r.incident_id=%s")
            params.append(incident_id)
        if status is not None:
            conditions.append("r.status=%s")
            params.append(status)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.connection() as cursor:
            total = cursor.execute("SELECT COUNT(*) FROM pilotage_diagnostics.requests r" + where, params).fetchone()["count"]
            rows = cursor.execute("SELECT r.id,r.incident_id,u.username AS requested_by,r.reason,r.status,r.created_at,r.claimed_at,r.finished_at,r.result_run_id FROM pilotage_diagnostics.requests r JOIN pilotage_auth.users u ON u.id=r.actor_id" + where + " ORDER BY r.created_at DESC,r.id DESC LIMIT %s OFFSET %s", (*params, limit, offset)).fetchall()
            return {"items": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset, "has_more": offset + len(rows) < total}

    def detail(self, identifier):
        if not isinstance(identifier, str) or re.fullmatch("[0-9a-f]{32}", identifier) is None:
            raise ValueError("Identifiant invalide.")
        with self.connection() as cursor:
            row = cursor.execute("SELECT r.id,r.incident_id,u.username AS requested_by,r.reason,r.status,r.created_at,r.claimed_at,r.finished_at,r.result_run_id FROM pilotage_diagnostics.requests r JOIN pilotage_auth.users u ON u.id=r.actor_id WHERE r.id=%s", (identifier,)).fetchone()
            return dict(row) if row else None
