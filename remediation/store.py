"""Audit de remediation separe, avec reservation durable avant toute action."""
import sqlite3
from pathlib import Path

try:
    from .diagnostics_store import DiagnosticStore, DiagnosticStorageError
except ImportError:
    from diagnostics.store import DiagnosticStore, DiagnosticStorageError

ACTION = "start_php_stopped"
PROFILE_ACTION = "apply_php_fpm_profile"
ACTIONS = (ACTION, PROFILE_ACTION)


class RemediationStore(DiagnosticStore):
    database_name = "remediation"

    def __init__(self, directory, *, database_config=None):
        super().__init__(directory, database_config=database_config)
        self.path = Path(directory) / "remediations.sqlite3"

    def initialize(self):
        changed = super().initialize()
        with self.connect(write=changed) as connection:
            if changed:
                connection.execute("""CREATE TABLE claims (
                    incident_id INTEGER PRIMARY KEY, action_id TEXT NOT NULL,
                    run_id TEXT NOT NULL UNIQUE, claimed_at REAL NOT NULL,
                    container_id TEXT NOT NULL)""")
            else:
                connection.execute("SELECT incident_id FROM claims LIMIT 1")
        return changed

    def reserve(self, identifier, incident_id, now, container_id, *, action_id=ACTION):
        if action_id not in ACTIONS:
            raise ValueError("Correction non autorisee.")
        with self.connect(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute("SELECT incident_id,status FROM runs WHERE id=?", (identifier,)).fetchone()
            if run is None or run["incident_id"] != incident_id or run["status"] != "running":
                raise DiagnosticStorageError("Execution non active.")
            if connection.execute("SELECT 1 FROM claims WHERE incident_id=?", (incident_id,)).fetchone():
                return "already_attempted_for_incident"
            last = connection.execute("SELECT MAX(claimed_at) FROM claims").fetchone()[0]
            if last is not None and now - last < 300:
                return "cooldown_or_clock_inconsistent"
            connection.execute("INSERT INTO claims VALUES(?,?,?,?,?)", (incident_id, action_id, identifier, now, container_id))
            connection.execute("UPDATE runs SET action_id=? WHERE id=?", (action_id, identifier))
        return None

    def detail(self, identifier):
        result = super().detail(identifier)
        if result:
            try:
                with self.connect() as connection:
                    row = connection.execute("SELECT * FROM claims WHERE run_id=?", (identifier,)).fetchone()
                    result["action_reservation"] = dict(row) if row else None
            except sqlite3.Error as exc:
                raise DiagnosticStorageError("Lecture de la reservation impossible.") from exc
        return result

    def finish(self, identifier, status, now, **kwargs):
        decision = kwargs.get("decision")
        if decision:
            action = decision.get("correction_action", ACTION)
            if action not in ACTIONS:
                raise ValueError("Correction non autorisee.")
            kwargs["decision"] = {**decision, "source_action_id": decision["action_id"], "action_id": action}
        return super().finish(identifier, status, now, **kwargs)

    def summary(self):
        result = super().summary()
        result["mode"] = "manual_guarded"
        result["action"] = ACTION
        result["actions"] = list(ACTIONS)
        result["automatic_execution_enabled"] = False
        result["semantics"] = {
            "completed": "Controle apres correction confirme ; voir le rapport et les mesures propres a l'action.",
            "reservation": "Intention d'action enregistree avant Docker ; ne prouve pas son execution.",
            "running": "Etat incomplet si le processus a subi une interruption brutale.",
            "action": "Une seule tentative par incident ; lancement manuel sur le serveur."}
        return result
