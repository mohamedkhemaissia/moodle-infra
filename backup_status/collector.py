"""Publier un etat sans demarrer ni interrompre aucun service."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .status import age_limit, encoded_time, marker, validate_document
from . import pilotage

PROPERTIES = ("Id", "LoadState", "ActiveState", "UnitFileState", "Result",
              "ExecMainStatus", "ExecMainStartTimestamp")


def unit_states(project, run=subprocess.run):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}", project):
        raise ValueError("Nom de projet invalide.")
    service, timer = project + "-backup.service", project + "-backup.timer"
    try:
        command = run(["/usr/bin/systemctl", "show", "--no-pager",
                       "--property=" + ",".join(PROPERTIES), service, timer],
                      capture_output=True, text=True, timeout=15, check=False,
                      env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        if command.returncode or len(command.stdout) > 16384:
            raise ValueError("Lecture indisponible.")
        units = {}
        for block in command.stdout.strip().split("\n\n"):
            values = {}
            for line in block.splitlines():
                key, separator, value = line.partition("=")
                if not separator or key not in PROPERTIES or key in values:
                    raise ValueError("Reponse invalide.")
                values[key] = value
            identifier = values.get("Id")
            if identifier not in (service, timer) or identifier in units:
                raise ValueError("Unite inattendue.")
            units[identifier] = values
        if set(units) != {service, timer}:
            raise ValueError("Reponse incomplete.")
    except (OSError, ValueError, subprocess.TimeoutExpired, UnicodeError):
        return {"state": "unknown", "enabled": None}, {"state": "unknown", "last_result": "unknown"}
    s, t = units[service], units[timer]
    schedule = {"state": "unknown", "enabled": None}
    if t.get("LoadState") == "loaded":
        state = t.get("ActiveState")
        schedule["state"] = "active" if state == "active" else (
            "inactive" if state in ("inactive", "failed") else "unknown")
        enabled = t.get("UnitFileState")
        schedule["enabled"] = True if enabled in ("enabled", "enabled-runtime") else (
            False if enabled in ("disabled", "masked", "masked-runtime") else None)
    job = {"state": "unknown", "last_result": "unknown"}
    if s.get("LoadState") == "loaded":
        state = s.get("ActiveState")
        job["state"] = "running" if state in ("active", "activating", "deactivating") else (
            "idle" if state in ("inactive", "failed") else "unknown")
        if s.get("ExecMainStartTimestamp") and job["state"] == "idle":
            if s.get("Result") == "success" and s.get("ExecMainStatus") == "0":
                job["last_result"] = "success"
            elif s.get("Result") not in (None, "", "success") or s.get("ExecMainStatus") not in (None, "", "0"):
                job["last_result"] = "failed"
    return schedule, job


def collect(marker_path, project, max_age_hours, now, run=subprocess.run):
    age_limit(max_age_hours)
    schedule, job = unit_states(project, run)
    value = {"schema_version": 1, "observed_at": encoded_time(now),
             "marker": marker(marker_path, now), "policy": {"max_age_hours": max_age_hours},
             "schedule": schedule, "job": job}
    return validate_document(value)


def publish(path, value, validator=validate_document):
    validator(value)
    path = Path(path)
    if path.is_symlink():
        raise ValueError("La destination est un lien.")
    descriptor, temporary = tempfile.mkstemp(prefix=".status-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(value, target, allow_nan=False, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
            os.fchmod(target.fileno(), 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--marker", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--max-age-hours", type=float, default=24)
    parser.add_argument("--pilotage-root")
    parser.add_argument("--pilotage-max-age-hours", type=float, default=24)
    args = parser.parse_args()
    try:
        now = datetime.now(timezone.utc)
        value = collect(args.marker, args.project, args.max_age_hours, now)
        if args.manual:
            value["schedule"] = {"state": "inactive", "enabled": False}
            value["job"] = {"state": "idle", "last_result": "unknown"}
        publish(args.output, value)
        if args.pilotage_root:
            schedule, job = unit_states("moodle-pilotage")
            pg = pilotage.collect(args.pilotage_root, args.pilotage_max_age_hours, now, schedule, job)
            publish(Path(args.output).with_name("pilotage.json"), pg, pilotage.validate_document)
    except (OSError, ValueError):
        parser.exit(1, "Publication du suivi impossible.\n")
    print(json.dumps({"status": "published", "marker_status": value["marker"]["status"]}))


if __name__ == "__main__":
    main()
