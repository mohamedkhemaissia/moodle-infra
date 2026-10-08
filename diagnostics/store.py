"""Journal dedie aux diagnostics ; lecteur API sans creation ni migration."""
import json
import math
import re
import sqlite3
from pathlib import Path
from pilotage.db import Connection as PostgreSQLConnection, load_config

APPLICATION_ID = 0x4D444941
STATUSES = {"running", "completed", "blocked", "failed", "timeout", "canceled", "interrupted"}
TERMINAL = STATUSES - {"running"}
MAX_JSON_BYTES = 262144


class DiagnosticStorageError(Exception):
    pass


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Cle JSON dupliquee.")
            result[key] = value
        return result
    def invalid(value):
        raise ValueError("Nombre JSON non fini.")
    if len(text.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("JSON trop volumineux.")
    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)


def encode(value):
    text = json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True)
    strict_json(text)
    return text


def run_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("Identifiant de diagnostic invalide.")
    return value


class DiagnosticStore:
    database_name = "diagnostics"

    def __init__(self, directory, *, database_config=None):
        self.path = Path(directory) / "diagnostics.sqlite3"
        self.database = load_config(database_config)

    def connect(self, *, write=False):
        try:
            if self.database is not None:
                return PostgreSQLConnection(self.database, self.database_name, write=write)
            if self.path.is_symlink() or not self.path.is_file():
                raise DiagnosticStorageError("Journal absent ou invalide.")
            connection = sqlite3.connect(self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                         uri=True, timeout=3)
            if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID or \
                    connection.execute("PRAGMA user_version").fetchone()[0] != 1:
                connection.close()
                raise DiagnosticStorageError("Version du journal incompatible.")
            connection.row_factory = sqlite3.Row
            if not write:
                connection.execute("PRAGMA query_only=ON")
            return connection
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise DiagnosticStorageError("Journal inaccessible.") from exc

    def initialize(self):
        # Pas de migration destructive ; l'API n'appelle jamais cette methode.
        if self.database is not None:
            with self.connect() as connection:
                connection.execute("SELECT id FROM runs LIMIT 1")
            return False
        if self.path.exists():
            with self.connect() as connection:
                connection.execute("SELECT id FROM runs LIMIT 1")
            return False
        if self.path.is_symlink():
            raise DiagnosticStorageError("Chemin du journal invalide.")
        try:
            with sqlite3.connect(self.path) as connection:
                connection.executescript(f"""
                PRAGMA application_id={APPLICATION_ID};
                PRAGMA user_version=1;
                CREATE TABLE runs (
                    id TEXT PRIMARY KEY, incident_id INTEGER NOT NULL,
                    action_id TEXT, status TEXT NOT NULL, started_at REAL NOT NULL,
                    finished_at REAL, decision_json TEXT, report_json TEXT,
                    events_json TEXT, runner_status TEXT, runner_rc INTEGER
                );
                CREATE INDEX runs_incident ON runs(incident_id, started_at);
                """)
            return True
        except (sqlite3.Error, OSError) as exc:
            raise DiagnosticStorageError("Initialisation impossible.") from exc

    def start(self, identifier, incident_id, now):
        run_id(identifier)
        if type(incident_id) is not int or not 1 <= incident_id <= 9223372036854775807:
            raise ValueError("Incident invalide.")
        if not math.isfinite(now):
            raise ValueError("Date invalide.")
        try:
            with self.connect(write=True) as connection:
                connection.execute("INSERT INTO runs(id, incident_id, status, started_at) VALUES(?,?,?,?)",
                                   (identifier, incident_id, "running", now))
        except sqlite3.Error as exc:
            raise DiagnosticStorageError("Ecriture impossible.") from exc

    def finish(self, identifier, status, now, *, decision=None, report=None, events=None,
               runner_status=None, runner_rc=None):
        run_id(identifier)
        if status not in TERMINAL or not math.isfinite(now):
            raise ValueError("Resultat invalide.")
        action = (decision or {}).get("action_id")
        values = (action, status, now, encode(decision), encode(report), encode(events or []),
                  runner_status, runner_rc, identifier)
        try:
            with self.connect(write=True) as connection:
                row = connection.execute("SELECT status FROM runs WHERE id=?", (identifier,)).fetchone()
                if row is None or row[0] != "running":
                    raise DiagnosticStorageError("Transition du diagnostic invalide.")
                connection.execute("""UPDATE runs SET action_id=?, status=?, finished_at=?,
                    decision_json=?, report_json=?, events_json=?, runner_status=?, runner_rc=? WHERE id=?""", values)
        except sqlite3.Error as exc:
            raise DiagnosticStorageError("Ecriture impossible.") from exc

    @staticmethod
    def decode(row, *, full=True):
        item = dict(row)
        if item.get("status") not in STATUSES:
            raise DiagnosticStorageError("Etat du journal invalide.")
        try:
            run_id(item["id"])
            for field in ("decision", "report", "events"):
                raw = item.pop(field + "_json", None)
                if full:
                    item[field] = strict_json(raw) if raw is not None else None
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            raise DiagnosticStorageError("Contenu du journal invalide.") from exc
        return item

    def detail(self, identifier):
        run_id(identifier)
        try:
            with self.connect() as connection:
                row = connection.execute("SELECT * FROM runs WHERE id=?", (identifier,)).fetchone()
                return self.decode(row) if row else None
        except sqlite3.Error as exc:
            raise DiagnosticStorageError("Lecture impossible.") from exc

    def listing(self, *, incident_id=None, limit=50, offset=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
            raise ValueError("Pagination invalide.")
        if incident_id is not None and (type(incident_id) is not int or incident_id < 1):
            raise ValueError("Incident invalide.")
        where = "WHERE incident_id=?" if incident_id is not None else ""
        args = (incident_id,) if incident_id is not None else ()
        try:
            with self.connect() as connection:
                connection.execute("BEGIN")
                count = connection.execute("SELECT COUNT(*) FROM runs " + where, args).fetchone()[0]
                rows = connection.execute("SELECT * FROM runs " + where + " ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
                                          args + (limit, offset)).fetchall()
                return {"items": [self.decode(row, full=False) for row in rows], "total": count,
                        "limit": limit, "offset": offset, "has_more": offset + len(rows) < count}
        except sqlite3.Error as exc:
            raise DiagnosticStorageError("Lecture impossible.") from exc

    def summary(self):
        try:
            with self.connect() as connection:
                counts = dict(connection.execute("SELECT status, COUNT(*) FROM runs GROUP BY status").fetchall())
            return {"status": "available", "mode": "manual_read_only", "counts": counts,
                    "semantics": {"completed": "Verifications executees ; ne signifie pas que le service est sain.",
                                  "running": "Execution demarree ; peut etre interrompue si le processus a ete arrete.",
                                  "action": "Consultation uniquement ; lancement par la commande locale de l'operateur."}}
        except sqlite3.Error as exc:
            raise DiagnosticStorageError("Lecture impossible.") from exc
