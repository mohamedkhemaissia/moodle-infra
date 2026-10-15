#!/usr/bin/env python3
"""Worker root minimal pour les sauvegardes completes du pilote Codespaces."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid


ROOT = Path("/var/lib/moodle-site-operations")
REQUESTS = ROOT / "requests"
PROCESSING = ROOT / "processing"
OPERATIONS = ROOT / "operations"
POINTS = ROOT / "points.json"
MARKER = Path("/opt/moodle/backups/last-success.txt")
BACKUP = "/usr/local/sbin/moodle-site-backup"
RESTORE = "/usr/local/sbin/moodle-site-restore"
RESTIC = "/usr/local/sbin/moodle-site-restic"
IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
SNAPSHOT = re.compile(r"^[0-9a-f]{64}$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o660)
    os.chown(temporary, 0, 10001)
    os.replace(temporary, path)


def read_json(path: Path, maximum: int = 65536) -> dict:
    info = path.lstat()
    if path.is_symlink() or not path.is_file() or info.st_size > maximum:
        raise ValueError("invalid_request_file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid_request")
    return value


def bounded(output: str) -> str:
    return "".join(character for character in output[-1500:] if character >= " " or character in "\n\t").strip()


def run(command: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=3600, check=False, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
        return result.returncode, bounded(result.stdout)
    except subprocess.TimeoutExpired as exc:
        return 124, bounded((exc.stdout or "") if isinstance(exc.stdout, str) else "") or "operation_timeout"


def marker() -> dict:
    try:
        values = dict(line.split("=", 1) for line in MARKER.read_text(encoding="utf-8").splitlines() if "=" in line)
        if not SNAPSHOT.fullmatch(values.get("snapshot", "")):
            return {}
        datetime.fromisoformat(values["completed_at_utc"].replace("Z", "+00:00"))
        return values
    except (OSError, KeyError, ValueError):
        return {}


def refresh_points() -> None:
    result = subprocess.run([RESTIC, "snapshots", "--json", "--tag", "moodle-pilote-full"], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False,
                            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
    if result.returncode != 0:
        raise RuntimeError("restic_catalog_failed")
    values = json.loads(result.stdout or "[]")
    if not isinstance(values, list):
        raise RuntimeError("restic_catalog_invalid")
    points = []
    for value in values:
        identifier = value.get("id") if isinstance(value, dict) else None
        created = value.get("time") if isinstance(value, dict) else None
        if SNAPSHOT.fullmatch(identifier or "") and isinstance(created, str):
            points.append({"id": identifier, "created_at": created})
    points.sort(key=lambda item: item["created_at"], reverse=True)
    write_json(POINTS, {"status": "available", "generated_at": now(), "latest_success": marker() or None,
                        "points": points[:30]})


def finish(operation: dict, status: str, reason: str | None, message: str) -> None:
    operation.update(status=status, reason_code=reason, message=message[:1500], finished_at=now(), updated_at=now())
    write_json(OPERATIONS / f"{operation['id']}.json", operation)


def execute(operation: dict) -> None:
    operation.update(status="running", started_at=now(), updated_at=now())
    write_json(OPERATIONS / f"{operation['id']}.json", operation)
    started = time.monotonic()
    if operation["kind"] == "backup":
        command = [BACKUP]
    else:
        snapshot = operation.get("snapshot_id", "")
        if not SNAPSHOT.fullmatch(snapshot):
            finish(operation, "failed", "invalid_snapshot", "Point de reprise invalide.")
            return
        command = [RESTORE, snapshot, "--confirmer-restauration"]
    status, output = run(command)
    operation["duration_seconds"] = round(time.monotonic() - started, 3)
    if status == 0:
        refresh_points()
        finish(operation, "completed", None,
               "Sauvegarde terminee." if operation["kind"] == "backup" else "Restauration terminee et Moodle redemarre.")
    else:
        finish(operation, "failed", "backup_failed" if operation["kind"] == "backup" else "restore_failed",
               output or "Operation en echec.")


def recover_interrupted() -> None:
    for path in PROCESSING.glob("*.json"):
        try:
            operation = read_json(path)
            if IDENTIFIER.fullmatch(operation.get("id", "")):
                finish(operation, "interrupted", "worker_restarted", "Operation interrompue par un redemarrage.")
        except Exception:
            pass
        path.unlink(missing_ok=True)


def process_next() -> bool:
    for queued in sorted(REQUESTS.glob("*.json")):
        if not IDENTIFIER.fullmatch(queued.stem):
            continue
        processing = PROCESSING / queued.name
        try:
            queued.rename(processing)
            operation = read_json(processing)
            if operation.get("id") != queued.stem or operation.get("kind") not in ("backup", "restore"):
                raise ValueError("invalid_request")
            execute(operation)
        except Exception as exc:
            operation = locals().get("operation")
            if isinstance(operation, dict) and IDENTIFIER.fullmatch(operation.get("id", "")):
                finish(operation, "failed", "worker_error", bounded(str(exc)) or "Operation invalide.")
        finally:
            processing.unlink(missing_ok=True)
        return True
    return False


def schedule_due(interval: int) -> bool:
    value = marker()
    if not value:
        return True
    completed = datetime.fromisoformat(value["completed_at_utc"].replace("Z", "+00:00")).timestamp()
    return time.time() - completed >= interval


def scheduled_backup() -> None:
    identifier = uuid.uuid4().hex
    stamp = now()
    operation = {"id": identifier, "client_key": identifier, "kind": "backup", "status": "queued",
                 "requested_by": {"id": 0, "username": "system"}, "snapshot_id": None,
                 "created_at": stamp, "updated_at": stamp, "started_at": None, "finished_at": None,
                 "reason_code": None, "message": None, "duration_seconds": None}
    write_json(OPERATIONS / f"{identifier}.json", operation)
    execute(operation)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=86400)
    arguments = parser.parse_args()
    if arguments.healthcheck:
        active = any(PROCESSING.glob("*.json"))
        return 0 if active or (POINTS.is_file() and time.time() - POINTS.stat().st_mtime <= 180) else 1
    if arguments.interval_seconds < 3600:
        raise SystemExit("interval_too_short")
    recover_interrupted()
    next_catalog = 0.0
    # Laisser le deploiement se terminer avant toute sauvegarde planifiee.
    next_schedule = time.monotonic() + 600
    while True:
        try:
            if process_next():
                next_catalog = 0
            current = time.monotonic()
            if current >= next_catalog:
                refresh_points()
                next_catalog = current + 60
            if current >= next_schedule:
                if schedule_due(arguments.interval_seconds):
                    scheduled_backup()
                next_schedule = current + 300
        except Exception as exc:
            print(f"{now()} {bounded(str(exc))}", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
