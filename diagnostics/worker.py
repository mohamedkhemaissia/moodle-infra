"""Worker de la file de diagnostics guides ; les controles restent en lecture seule."""
import argparse
import json
from pathlib import Path
import signal
import sys
import threading
import uuid

from pilotage.diagnostic_requests import (
    DiagnosticRequestConflict,
    DiagnosticRequestStorageError,
    DiagnosticRequestStore,
    TERMINAL,
)

from .collector import load_config


CODE_DIR = Path(__file__).resolve().parent.parent
CONFIG = CODE_DIR.parent / "config.json"


def worker_identifier():
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text().strip().replace("-", "")
        if len(value) == 32 and all(character in "0123456789abcdef" for character in value):
            return value
    except OSError:
        pass
    return uuid.uuid4().hex


def process(store, config, config_path, worker_id, *, execute_function=None):
    if execute_function is None:
        # fcntl et Runner sont propres au worker Linux ; garder l'import hors
        # des lecteurs et des tests de contrat multiplateformes.
        from .run import execute as execute_function
    job = store.claim(worker_id)
    if job is None:
        return {"status": "idle", "mode": "read_only"}
    try:
        result, _ = execute_function(
            config,
            config_path,
            job["incident_id"],
            run_identifier=job["run_id"],
        )
        status = result.get("status")
        if status == "busy":
            store.requeue(job["id"], job["claim_token"])
            return {"status": "waiting", "reason": "diagnostic_already_running", "mode": "read_only"}
        if status not in TERMINAL:
            status = "failed"
        reason = result.get("reason")
        if reason is None and status != "completed":
            reason = "diagnostic_" + status
        request = store.finish(job["id"], job["claim_token"], status, reason_code=reason, result_available=True)
        return {"status": request["status"], "request_id": job["id"],
                "run_id": job["run_id"], "mode": "read_only"}
    except DiagnosticRequestConflict:
        # L'echeance a pu etre constatee par un autre worker. Ne jamais
        # remplacer un resultat terminal deja publie.
        return {"status": "interrupted", "request_id": job["id"], "mode": "read_only"}
    except Exception:
        try:
            store.finish(job["id"], job["claim_token"], "failed", reason_code="diagnostic_worker_failed")
        except (DiagnosticRequestConflict, DiagnosticRequestStorageError):
            pass
        return {"status": "failed", "request_id": job["id"], "mode": "read_only"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Traiter au plus une demande puis quitter.")
    parser.add_argument("--healthcheck", action="store_true", help="Verifier la configuration et PostgreSQL sans reserver de demande.")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()
    if args.once and args.healthcheck:
        parser.error("Choisir --once ou --healthcheck.")
    if not 0.2 <= args.poll_seconds <= 60:
        parser.error("Le delai doit etre compris entre 0,2 et 60 secondes.")
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    try:
        config = load_config(CONFIG)
        store = DiagnosticRequestStore()
        store.check()
        if args.healthcheck:
            print(json.dumps({"status": "available", "mode": "read_only", "queue": "postgresql"}))
            return 0
        identity = worker_identifier()
        while not stopping.is_set():
            result = process(store, config, CONFIG, identity)
            if args.once:
                print(json.dumps(result))
                return 0 if result["status"] in ("idle", "waiting", "completed") else 2
            stopping.wait(args.poll_seconds)
        return 0
    except (DiagnosticRequestStorageError, OSError, ValueError):
        print(json.dumps({"status": "unavailable", "mode": "read_only"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
