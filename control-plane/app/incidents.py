"""Incidents observes, transitions transactionnelles et consultation en lecture seule."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from urllib.parse import quote
from pilotage.db import Connection as PostgreSQLConnection, load_config

SCHEMA_VERSION = 1
APPLICATION_ID = 0x4D4F4F49
DB_NAME = "incidents.sqlite3"
POLICY = {"version": 1, "open_checks": 2, "resolve_checks": 3,
          "interval_seconds": 30, "max_gap_seconds": 90}
DEFINITIONS = {
    "prometheus_unavailable": ("Lecture de Prometheus indisponible", "data_quality", "warning"),
    "linux_collection": ("Collecte des ressources Linux degradee", "data_quality", "warning"),
    "moodle_collection": ("Collecte de la sonde Moodle degradee", "data_quality", "warning"),
    "moodle_https": ("Sonde HTTPS Moodle en echec", "availability", "critical"),
    "cpu_high": ("Consommation CPU elevee", "resources", "warning"),
    "memory_high": ("Consommation memoire elevee", "resources", "warning"),
    "disk_low": ("Espace disque disponible faible", "resources", "warning"),
    "anomaly_model": ("Modele d anomalies indisponible", "data_quality", "warning"),
    "analysis_history": ("Historique insuffisant pour l analyse des ressources", "data_quality", "warning"),
    "statistical_anomaly": ("Observation atypique des ressources", "statistical", "warning"),
    "php_pool_queue": ("File d'attente du pool PHP-FPM", "capacity", "warning"),
}
STATES = ("triggered", "clear", "hold", "unknown")


class IncidentStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class Observation:
    key: str
    state: str
    evidence: dict
    context: str = "policy-v1"


def encoded(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
    if len(text.encode()) > 32 * 1024:
        raise ValueError("Preuve d'observation trop volumineuse.")
    return text


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z") if value is not None else None


SCHEMA = (
    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE signal_state (
        key TEXT PRIMARY KEY, context TEXT NOT NULL, last_point INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('triggered','clear','hold','unknown')),
        bad_count INTEGER NOT NULL, good_count INTEGER NOT NULL,
        first_bad_at INTEGER, evidence_json TEXT NOT NULL)""",
    """CREATE TABLE incidents (
        id INTEGER PRIMARY KEY AUTOINCREMENT, signal_key TEXT NOT NULL,
        title TEXT NOT NULL, category TEXT NOT NULL, severity TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('open','resolved')),
        first_observed_at INTEGER NOT NULL, opened_at INTEGER NOT NULL,
        last_observed_at INTEGER NOT NULL, resolved_at INTEGER,
        trigger_observations INTEGER NOT NULL, unknown_observations INTEGER NOT NULL DEFAULT 0,
        latest_state TEXT NOT NULL, context TEXT NOT NULL,
        evidence_json TEXT NOT NULL, policy_json TEXT NOT NULL)""",
    "CREATE UNIQUE INDEX one_open_incident ON incidents(signal_key) WHERE status='open'",
    "CREATE INDEX incidents_status ON incidents(status,id)",
    """CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_id INTEGER NOT NULL REFERENCES incidents(id),
        kind TEXT NOT NULL, observed_at INTEGER NOT NULL, evidence_json TEXT NOT NULL)""",
    "CREATE INDEX events_incident ON events(incident_id,id)",
)


class IncidentStore:
    def __init__(self, directory, *, database_config=None, decision_journal_enabled=False):
        self.path = Path(directory) / DB_NAME
        self.database = load_config(database_config)
        self.decision_journal = None
        if decision_journal_enabled:
            from pilotage.decision_journal import DecisionJournal
            self.decision_journal = DecisionJournal(self)

    @contextmanager
    def connection(self, *, write=False):
        connection = None
        try:
            if self.database is not None:
                connection = PostgreSQLConnection(self.database, "incidents", write=write)
            elif write:
                connection = sqlite3.connect(self.path, timeout=1.5, isolation_level=None)
                connection.execute("PRAGMA synchronous=FULL")
            else:
                uri = "file:" + quote(str(self.path.resolve()), safe="/") + "?mode=ro"
                connection = sqlite3.connect(uri, uri=True, timeout=1.5, isolation_level=None)
                connection.execute("PRAGMA query_only=ON")
            if self.database is None:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
            raise IncidentStorageError("Stockage des incidents indisponible ou incompatible.") from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def check_schema(connection):
        if isinstance(connection, PostgreSQLConnection):
            connection.check_schema()
            return
        if (connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION):
            raise IncidentStorageError("Version du stockage des incidents incompatible.")

    def initialize(self):
        if self.database is not None:
            with self.connection() as connection:
                self.check_schema(connection)
            if self.decision_journal is not None:
                self.decision_journal.initialize()
            return
        if not self.path.parent.is_dir():
            raise IncidentStorageError("Repertoire persistant des incidents absent.")
        with self.connection(write=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            app = connection.execute("PRAGMA application_id").fetchone()[0]
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            if version == 0 and app == 0 and not tables:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in SCHEMA:
                        connection.execute(statement)
                    connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
            else:
                self.check_schema(connection)
            if connection.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise IncidentStorageError("Mode de journalisation incompatible.")

    @staticmethod
    def event(connection, incident_id, kind, point, evidence):
        connection.execute("INSERT INTO events(incident_id,kind,observed_at,evidence_json) VALUES(?,?,?,?)",
                           (incident_id, kind, point, encoded(evidence)))

    def apply(self, point, observations, *, completed_at, diagnostics=None):
        if (type(point) is not int or point < 0 or point % POLICY["interval_seconds"]
                or type(completed_at) not in (int, float) or not math.isfinite(completed_at) or completed_at < point):
            raise ValueError("Date d'observation invalide.")
        if (not isinstance(observations, list) or any(not isinstance(o, Observation) for o in observations)
                or {o.key for o in observations} != set(DEFINITIONS) or len(observations) != len(DEFINITIONS)):
            raise ValueError("Tous les signaux attendus doivent etre observes une fois chacun.")
        for observation in observations:
            if (not isinstance(observation, Observation) or observation.state not in STATES
                    or not isinstance(observation.evidence, dict) or not isinstance(observation.context, str)
                    or not 1 <= len(observation.context) <= 256):
                raise ValueError("Observation invalide.")
            encoded(observation.evidence)
        encoded(diagnostics or {})
        processed = 0
        with self.connection(write=True) as connection:
            self.check_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                for observation in observations:
                    processed += self.apply_one(connection, point, observation)
                newest = connection.execute("SELECT max(last_point) FROM signal_state").fetchone()[0]
                collector = {"clock_inconsistent": newest is not None and point < newest, "mode": "observation", "last_completed_epoch": completed_at,
                             "last_completed_at": iso(completed_at), "input_epoch": point, "input_evaluated_at": iso(point),
                             "processed_signals": processed, "diagnostics": diagnostics or {}, "policy": POLICY}
                connection.execute("INSERT INTO metadata(key,value) VALUES('collector',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                   (encoded(collector),))
                if self.decision_journal is not None and processed == len(DEFINITIONS):
                    from .decisions import evaluate, policy_document
                    snapshot = self.snapshot(connection, completed_at, closed_at=point)
                    self.decision_journal.record(connection, evaluate(snapshot, completed_at), policy_document(),
                                                 point=point, completed_at=completed_at)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return processed

    def apply_one(self, connection, point, observation):
        previous = connection.execute("SELECT * FROM signal_state WHERE key=?", (observation.key,)).fetchone()
        if previous is not None and point <= previous["last_point"]:
            return 0
        active = connection.execute("SELECT * FROM incidents WHERE signal_key=? AND status='open'", (observation.key,)).fetchone()
        gap = previous is not None and point - previous["last_point"] > POLICY["max_gap_seconds"]
        changed_context = previous is not None and previous["context"] != observation.context
        bad = previous["bad_count"] if previous is not None and not gap and not changed_context else 0
        good = previous["good_count"] if previous is not None and not gap and not changed_context else 0
        first_bad = previous["first_bad_at"] if bad else None
        if active is not None:
            if gap:
                self.event(connection, active["id"], "observation_gap", point,
                           {"previous_observed_at": iso(previous["last_point"]), "max_gap_seconds": POLICY["max_gap_seconds"]})
            if changed_context:
                self.event(connection, active["id"], "context_changed", point,
                           {"previous_context": previous["context"], "context": observation.context})
            if observation.state == "unknown" and active["latest_state"] != "unknown":
                self.event(connection, active["id"], "observation_unavailable", point, observation.evidence)
            elif observation.state != "unknown" and active["latest_state"] == "unknown":
                self.event(connection, active["id"], "observation_resumed", point, observation.evidence)
        if observation.state == "triggered":
            bad, good = min(bad + 1, POLICY["open_checks"]), 0
            first_bad = point if first_bad is None else first_bad
            if active is None and bad >= POLICY["open_checks"]:
                title, category, severity = DEFINITIONS[observation.key]
                cursor = connection.execute("""INSERT INTO incidents(
                    signal_key,title,category,severity,status,first_observed_at,opened_at,last_observed_at,
                    trigger_observations,latest_state,context,evidence_json,policy_json)
                    VALUES(?,?,?,?,'open',?,?,?,?,?,?,?,?)""",
                    (observation.key,title,category,severity,first_bad,point,point,1,observation.state,
                     observation.context,encoded(observation.evidence),encoded(POLICY)))
                self.event(connection,cursor.lastrowid,"opened",point,observation.evidence)
                active = connection.execute("SELECT * FROM incidents WHERE id=?", (cursor.lastrowid,)).fetchone()
        elif observation.state == "clear":
            bad, first_bad, good = 0, None, min(good + 1, POLICY["resolve_checks"])
        else:
            bad, good, first_bad = 0, 0, None
        if active is not None:
            resolved = observation.state == "clear" and good >= POLICY["resolve_checks"]
            connection.execute("""UPDATE incidents SET status=?,last_observed_at=?,resolved_at=?,
                trigger_observations=trigger_observations+?,unknown_observations=unknown_observations+?,
                latest_state=?,context=?,evidence_json=? WHERE id=?""",
                ("resolved" if resolved else "open",point,point if resolved else None,
                 int(observation.state == "triggered"),int(observation.state == "unknown"),
                 observation.state,observation.context,encoded(observation.evidence),active["id"]))
            if resolved:
                self.event(connection,active["id"],"resolved",point,observation.evidence)
        connection.execute("""INSERT INTO signal_state(key,context,last_point,state,bad_count,good_count,first_bad_at,evidence_json)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET context=excluded.context,last_point=excluded.last_point,
            state=excluded.state,bad_count=excluded.bad_count,good_count=excluded.good_count,
            first_bad_at=excluded.first_bad_at,evidence_json=excluded.evidence_json""",
            (observation.key,observation.context,point,observation.state,bad,good,first_bad,encoded(observation.evidence)))
        return 1

    @staticmethod
    def incident(row):
        result = dict(row)
        for key in ("first_observed_at", "opened_at", "last_observed_at", "resolved_at"):
            result[key] = iso(result[key])
        result["evidence"] = json.loads(result.pop("evidence_json"))
        result["policy"] = json.loads(result.pop("policy_json"))
        return result

    def listing(self, *, status=None, limit=50, offset=0):
        if status not in (None,"open","resolved") or type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100_000:
            raise ValueError("Pagination ou statut invalide.")
        clause, parameters = (" WHERE status=?",(status,)) if status is not None else ("",())
        with self.connection() as connection:
            self.check_schema(connection)
            connection.execute("BEGIN")
            total = connection.execute("SELECT count(*) FROM incidents"+clause,parameters).fetchone()[0]
            rows = connection.execute("SELECT * FROM incidents"+clause+" ORDER BY id DESC LIMIT ? OFFSET ?",(*parameters,limit,offset)).fetchall()
            return {"items":[self.incident(row) for row in rows],"total":total,"limit":limit,"offset":offset,"has_more":offset+len(rows)<total}

    def detail(self, identifier):
        with self.connection() as connection:
            self.check_schema(connection)
            row = connection.execute("SELECT * FROM incidents WHERE id=?",(identifier,)).fetchone()
            return self.incident(row) if row is not None else None

    def events(self, identifier, *, limit=50, after_id=0):
        if type(limit) is not int or not 1 <= limit <= 100 or type(after_id) is not int or after_id < 0:
            raise ValueError("Pagination des evenements invalide.")
        with self.connection() as connection:
            self.check_schema(connection)
            connection.execute("BEGIN")
            if connection.execute("SELECT 1 FROM incidents WHERE id=?",(identifier,)).fetchone() is None:
                return None
            rows = connection.execute("SELECT * FROM events WHERE incident_id=? AND id>? ORDER BY id LIMIT ?",(identifier,after_id,limit+1)).fetchall()
            items = []
            for row in rows[:limit]:
                item = dict(row)
                item["observed_at"] = iso(item["observed_at"])
                item["evidence"] = json.loads(item.pop("evidence_json"))
                items.append(item)
            return {"items":items,"has_more":len(rows)>limit,"next_after_id":items[-1]["id"] if items else after_id}

    def summary(self, now):
        with self.connection() as connection:
            self.check_schema(connection)
            connection.execute("BEGIN")
            counts = {"open":0,"resolved":0}
            for row in connection.execute("SELECT status,count(*) AS count FROM incidents GROUP BY status"):
                counts[row["status"]] = row["count"]
            collector = self.read_collector(connection, now)
            signals = [{"key":row["key"],"state":row["state"],"last_observed_at":iso(row["last_point"]),
                        "pending_trigger_checks":row["bad_count"],"pending_recovery_checks":row["good_count"]}
                       for row in connection.execute("SELECT * FROM signal_state ORDER BY key")]
            return {"status":"available","mode":"observation","evaluated_at":iso(now),"counts":counts,
                    "collector":collector,"signals":signals,"policy":POLICY,
                    "semantics":{"incident":"Signal observe et confirme par plusieurs controles ; cause non determinee.",
                                 "unknown":"Une observation inconnue ne resout aucun incident.",
                                 "action":"Aucune remediation executee par ce module.",
                                 "freshness":"Les etats des signaux ne decrivent pas le present si l observateur est obsolete."}}

    @staticmethod
    def read_collector(connection, now):
        row = connection.execute("SELECT value FROM metadata WHERE key='collector'").fetchone()
        collector = json.loads(row[0]) if row else None
        if collector is not None:
            if (not isinstance(collector,dict)
                    or type(collector.get("last_completed_epoch")) not in (int,float)
                    or not math.isfinite(collector["last_completed_epoch"])
                    or type(collector.get("input_epoch")) is not int
                    or collector["input_epoch"] < 0 or collector["input_epoch"] % POLICY["interval_seconds"]):
                raise IncidentStorageError("Metadonnees de l'observateur invalides.")
            age = now - collector.pop("last_completed_epoch")
            input_age = now - collector.pop("input_epoch")
            collector["age_seconds"] = round(age,3)
            collector["input_age_seconds"] = round(input_age,3)
            collector["status"] = ("clock_inconsistent" if age < 0 or input_age < 0 or collector.get("clock_inconsistent")
                                   else "stale" if max(age,input_age) > POLICY["max_gap_seconds"] else "running")
        return collector

    def decision_snapshot(self, now, identifier=None):
        """Lire episodes et signaux sur le meme instantane, sans ecriture."""
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ValueError("Date d'evaluation invalide.")
        if identifier is not None and (type(identifier) is not int or not 1 <= identifier < 2**63):
            raise ValueError("Identifiant d'incident invalide.")
        with self.connection() as connection:
            self.check_schema(connection)
            connection.execute("BEGIN")
            return self.snapshot(connection, now, identifier=identifier)

    def snapshot(self, connection, now, *, identifier=None, closed_at=None):
        collector = self.read_collector(connection, now)
        signals = {}
        for row in connection.execute("SELECT * FROM signal_state ORDER BY key"):
            item = dict(row)
            item["last_observed_at"] = iso(item.pop("last_point"))
            item["evidence"] = json.loads(item.pop("evidence_json"))
            item.pop("first_bad_at")
            signals[item["key"]] = item
        clause, parameters = ("status='open'", ()) if identifier is None else ("id=?", (identifier,))
        if closed_at is not None:
            clause, parameters = "(status='open' OR resolved_at=?)", (closed_at,)
        rows = connection.execute("SELECT * FROM incidents WHERE " + clause + " ORDER BY id DESC LIMIT ?",
                                  (*parameters, len(DEFINITIONS) + 1)).fetchall()
        if len(rows) > len(DEFINITIONS):
            raise IncidentStorageError("Nombre d'incidents actifs incoherent.")
        active = connection.execute("SELECT id,signal_key FROM incidents WHERE status='open' ORDER BY id").fetchall()
        return {"collector": collector, "signals": signals, "incidents": [self.incident(row) for row in rows],
                "open_incidents": [dict(row) for row in active]}
