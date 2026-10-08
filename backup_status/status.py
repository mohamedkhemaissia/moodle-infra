"""Contrat public borne ; aucune lecture du depot Restic ni de ses secrets."""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat

MAX_BYTES = 8192
MAX_COLLECTION_AGE_SECONDS = 150
SNAPSHOT = re.compile(r"[0-9a-f]{64}")


def utc(value):
    if not isinstance(value, str):
        raise ValueError("Date absente.")
    result = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if result.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError("Date non canonique.")
    return result


def encoded_time(now):
    if now.tzinfo is None:
        raise ValueError("Une date avec fuseau est requise.")
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bounded_text(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("Fichier inexploitable.")
        with os.fdopen(fd, "rb", closefd=False) as source:
            data = source.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("Fichier trop grand.")
        return data.decode("utf-8")
    finally:
        os.close(fd)


def age_limit(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 720:
        raise ValueError("Seuil d'age invalide.")
    return value


def latest_point(value, observed_at):
    if not isinstance(value, dict) or set(value) != {"snapshot", "frozen_at_utc", "completed_at_utc"}:
        raise ValueError("Point de reprise invalide.")
    if not isinstance(value["snapshot"], str) or SNAPSHOT.fullmatch(value["snapshot"]) is None:
        raise ValueError("Reference invalide.")
    frozen, completed = utc(value["frozen_at_utc"]), utc(value["completed_at_utc"])
    if frozen > completed or completed > observed_at:
        raise ValueError("Dates incoherentes.")
    return dict(value)


def marker(path, now):
    try:
        records = {}
        for line in bounded_text(path).splitlines():
            if not line.strip():
                continue
            key, separator, value = line.partition("=")
            if not separator or key in records:
                raise ValueError("Marqueur invalide.")
            records[key] = value
        return {"status": "recorded", "latest": latest_point(records, now)}
    except FileNotFoundError:
        return {"status": "missing", "latest": None}
    except (OSError, UnicodeError, ValueError):
        return {"status": "invalid", "latest": None}


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Cle dupliquee.")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("Nombre non standard.")


def validate_document(value):
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "observed_at", "marker", "policy", "schedule", "job"
    } or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Format incompatible.")
    observed = utc(value["observed_at"])
    policy, record, schedule, job = (value[name] for name in ("policy", "marker", "schedule", "job"))
    if not isinstance(policy, dict) or set(policy) != {"max_age_hours"}:
        raise ValueError("Politique invalide.")
    age_limit(policy["max_age_hours"])
    if not isinstance(record, dict) or set(record) != {"status", "latest"}:
        raise ValueError("Marqueur invalide.")
    if record["status"] == "recorded":
        latest_point(record["latest"], observed)
    elif record["status"] not in ("missing", "invalid") or record["latest"] is not None:
        raise ValueError("Etat invalide.")
    if (not isinstance(schedule, dict) or set(schedule) != {"state", "enabled"}
            or schedule["state"] not in ("active", "inactive", "unknown")
            or (schedule["enabled"] is not None and type(schedule["enabled"]) is not bool)):
        raise ValueError("Planification invalide.")
    if (not isinstance(job, dict) or set(job) != {"state", "last_result"}
            or job["state"] not in ("running", "idle", "unknown")
            or job["last_result"] not in ("success", "failed", "unknown")):
        raise ValueError("Traitement invalide.")
    return value


def summary(path, now):
    result = {
        "status": "unavailable", "mode": "read_only", "evaluated_at": encoded_time(now),
        "backup_state": "unknown", "latest_success": None, "policy": None,
        "schedule": {"state": "unknown", "enabled": None},
        "job": {"state": "unknown", "last_result": "unknown"},
        "collection": {"status": "missing", "observed_at": None, "age_seconds": None,
                       "max_age_seconds": MAX_COLLECTION_AGE_SECONDS},
        "reason": "status_file_missing",
        "verification": {"repository_integrity": "not_checked", "restoration": "not_checked"},
    }
    try:
        value = validate_document(json.loads(bounded_text(Path(path)),
            object_pairs_hook=_pairs, parse_constant=_constant))
    except FileNotFoundError:
        return result
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        result["reason"] = "status_file_invalid"
        result["collection"]["status"] = "invalid"
        return result
    observed = utc(value["observed_at"])
    age = (now - observed).total_seconds()
    result["collection"].update(observed_at=value["observed_at"], age_seconds=round(age, 3))
    if age < 0:
        result["reason"] = "clock_inconsistent"
        result["collection"]["status"] = "invalid"
        return result
    if age > MAX_COLLECTION_AGE_SECONDS:
        result["reason"] = "collection_stale"
        result["collection"]["status"] = "stale"
        return result
    result.update(status="available", reason=None, policy=dict(value["policy"]),
                  schedule=dict(value["schedule"]), job=dict(value["job"]))
    result["collection"]["status"] = "up"
    record = value["marker"]
    if record["status"] == "recorded":
        latest = dict(record["latest"])
        point_age = (now - utc(latest["frozen_at_utc"])).total_seconds() / 3600
        latest["age_hours"] = round(point_age, 3)
        latest["duration_seconds"] = (utc(latest["completed_at_utc"]) -
                                      utc(latest["frozen_at_utc"])).total_seconds()
        result["latest_success"] = latest
        result["backup_state"] = "overdue" if point_age > value["policy"]["max_age_hours"] else "recent"
    elif record["status"] == "missing":
        result.update(backup_state="missing", reason="success_marker_missing")
    else:
        result["reason"] = "success_marker_invalid"
    return result
