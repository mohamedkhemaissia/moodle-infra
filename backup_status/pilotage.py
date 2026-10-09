"""Projection publique des preuves PostgreSQL ; aucun acces au depot Restic.

Source partagee entre le collecteur (backup_status) et le contexte API (app).
"""
from datetime import datetime
import json
import math
from pathlib import Path
import re

if __package__ == "backup_status":
    from . import status as common
else:
    from . import backup_status as common

POINT_KEYS = {"snapshot", "frozen_at_utc", "completed_at_utc", "tables", "rows", "repository_check"}
RESTORE_KEYS = {"run_id", "snapshot", "status", "started_at", "completed_at", "captured_at",
                "duration_seconds", "tables_verified", "rows_verified", "sequence_check", "cleanup"}
MAX_REPORTS = 1024


def integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 9007199254740991:
        raise ValueError("Comptage invalide.")
    return value


def read_json(path):
    path = Path(path)
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Repertoire lie.")
    return json.loads(common.bounded_text(path), object_pairs_hook=common._pairs,
                      parse_constant=common._constant)


def point(value, observed):
    if not isinstance(value, dict) or set(value) != POINT_KEYS:
        raise ValueError("Point PostgreSQL incompatible.")
    common.latest_point({key: value[key] for key in ("snapshot", "frozen_at_utc", "completed_at_utc")}, observed)
    integer(value["tables"], 1)
    integer(value["rows"])
    if value["repository_check"] != "passed":
        raise ValueError("Integrite non confirmee.")
    return value


def restore(value, observed):
    if not isinstance(value, dict) or set(value) != RESTORE_KEYS:
        raise ValueError("Rapport public incompatible.")
    if (not isinstance(value["run_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", value["run_id"])
            or not isinstance(value["snapshot"], str) or not common.SNAPSHOT.fullmatch(value["snapshot"])
            or value["status"] not in ("restore_validated", "failed", "inconclusive")):
        raise ValueError("Rapport incompatible.")
    started, completed = common.utc(value["started_at"]), common.utc(value["completed_at"])
    if started > completed or completed > observed:
        raise ValueError("Dates du test incoherentes.")
    captured = value["captured_at"]
    if captured is not None and common.utc(captured) > started:
        raise ValueError("Point du test incoherent.")
    duration = value["duration_seconds"]
    if duration is not None and (type(duration) not in (int, float)
            or not math.isfinite(duration) or duration < 0):
        raise ValueError("Duree invalide.")
    for key in ("tables_verified", "rows_verified"):
        if value[key] is not None:
            integer(value[key], 1 if key == "tables_verified" else 0)
    cleanup = value["cleanup"]
    if (not isinstance(cleanup, dict) or set(cleanup) != {"container_removed", "temporary_files_removed"}
            or any(type(item) is not bool for item in cleanup.values())
            or value["sequence_check"] not in ("passed", "not_verified")):
        raise ValueError("Verifications incompatibles.")
    if value["status"] == "restore_validated" and (
            captured is None or duration is None or value["tables_verified"] is None
            or value["rows_verified"] is None or value["sequence_check"] != "passed"
            or not all(cleanup.values())):
        raise ValueError("Restauration non prouvee.")
    return value


def read_marker(root, now):
    try:
        source = read_json(root / "last-success.json")
        if (not isinstance(source, dict) or source.get("format") != "moodle-pilotage-backup-v1"
                or source.get("status") != "backed_up"
                or common.utc(source["started_at"]) > common.utc(source["captured_at"])):
            raise ValueError("Marqueur incompatible.")
        latest = point({"snapshot": source["snapshot"], "frozen_at_utc": source["captured_at"],
            "completed_at_utc": source["completed_at"], "tables": source["tables"],
            "rows": source["rows"], "repository_check": source["repository_check"]}, now)
        return {"status": "recorded", "latest": latest}
    except FileNotFoundError:
        return {"status": "missing", "latest": None}
    except (OSError, ValueError, UnicodeError, KeyError, TypeError):
        return {"status": "invalid", "latest": None}


def read_restores(root, now):
    """La date du rapport, jamais le mtime, choisit le dernier test termine.

    Un fichier corrompu empeche de reutiliser une ancienne preuve de reussite.
    """
    try:
        directory = root / "reports"
        if directory.is_symlink() or any(parent.is_symlink() for parent in directory.parents):
            raise ValueError("Repertoire lie.")
        if not directory.exists():
            return {"status": "missing", "latest": None}
        latest, count = None, 0
        for path in directory.iterdir():
            if not path.name.startswith("restore-") or path.suffix != ".json":
                continue
            count += 1
            if count > MAX_REPORTS or not re.fullmatch(r"restore-[0-9a-f]{32}\.json", path.name):
                raise ValueError("Inventaire des rapports incompatible.")
            source = read_json(path)
            if (not isinstance(source, dict) or source.get("mode") != "isolated_restore_test"
                    or source.get("production_changed") is not False
                    or path.name != "restore-" + str(source.get("run_id")) + ".json"):
                raise ValueError("Test non isole.")
            value = {key: source.get(key) for key in RESTORE_KEYS}
            value["sequence_check"] = source.get("sequence_check", "not_verified")
            restore(value, now)
            if source["status"] == "restore_validated" and (
                    not isinstance(source.get("inventory_sha256"), str)
                    or not common.SNAPSHOT.fullmatch(source["inventory_sha256"])):
                raise ValueError("Inventaire non confirme.")
            if latest is None or (value["completed_at"], value["run_id"]) > (latest["completed_at"], latest["run_id"]):
                latest = value
        return {"status": "recorded" if latest else "missing", "latest": latest}
    except (OSError, ValueError, UnicodeError, KeyError, TypeError):
        return {"status": "invalid", "latest": None}


def validate_document(value):
    if not isinstance(value, dict) or set(value) != {
            "schema_version", "scope", "observed_at", "marker", "restore", "policy", "schedule", "job"
    } or value["scope"] != "pilotage_postgresql":
        raise ValueError("Format PostgreSQL incompatible.")
    observed = common.utc(value["observed_at"])
    base = {key: value[key] for key in ("schema_version", "observed_at", "marker", "policy", "schedule", "job")}
    record = value["marker"]
    if isinstance(record, dict) and record.get("status") == "recorded":
        point(record["latest"], observed)
        base["marker"] = {"status": "recorded", "latest": {key: record["latest"][key]
            for key in ("snapshot", "frozen_at_utc", "completed_at_utc")}}
        if set(record) != {"status", "latest"}:
            raise ValueError("Marqueur incompatible.")
    common.validate_document(base)
    record = value["restore"]
    if not isinstance(record, dict) or set(record) != {"status", "latest"}:
        raise ValueError("Etat du test incompatible.")
    if record["status"] == "recorded":
        restore(record["latest"], observed)
    elif record["status"] not in ("missing", "invalid") or record["latest"] is not None:
        raise ValueError("Etat du test incompatible.")
    return value


def collect(root, max_age_hours, now, schedule, job):
    common.age_limit(max_age_hours)
    value = {"schema_version": 1, "scope": "pilotage_postgresql", "observed_at": common.encoded_time(now),
             "marker": read_marker(Path(root), now), "restore": read_restores(Path(root), now),
             "policy": {"max_age_hours": max_age_hours}, "schedule": dict(schedule), "job": dict(job)}
    return validate_document(value)


def summary(path, now: datetime):
    result = common.empty_summary(now)
    result.update(scope="pilotage_postgresql", latest_restore=None,
                  restore_collection_status="unknown", restoration_state="unknown", same_snapshot=None)
    try:
        value = validate_document(read_json(path))
    except FileNotFoundError:
        return result
    except (OSError, ValueError, UnicodeError, KeyError, TypeError):
        result.update(reason="status_file_invalid")
        result["collection"]["status"] = "invalid"
        return result
    observed = common.utc(value["observed_at"])
    age = (now - observed).total_seconds()
    result["collection"].update(observed_at=value["observed_at"], age_seconds=round(age, 3))
    if age < 0 or age > common.MAX_COLLECTION_AGE_SECONDS:
        result["reason"] = "clock_inconsistent" if age < 0 else "collection_stale"
        result["collection"]["status"] = "invalid" if age < 0 else "stale"
        return result
    result.update(status="available", reason=None, policy=value["policy"], schedule=value["schedule"], job=value["job"])
    result["collection"]["status"] = "up"
    latest = value["marker"]["latest"]
    if latest is not None:
        hours = (now - common.utc(latest["frozen_at_utc"])).total_seconds() / 3600
        result["latest_success"] = {**latest, "age_hours": round(hours, 3), "duration_seconds":
            (common.utc(latest["completed_at_utc"]) - common.utc(latest["frozen_at_utc"])).total_seconds()}
        result["backup_state"] = "overdue" if hours > value["policy"]["max_age_hours"] else "recent"
        result["verification"]["repository_integrity"] = "passed_at_backup"
    else:
        missing = value["marker"]["status"] == "missing"
        result.update(backup_state="missing" if missing else "unknown", reason=
                      "success_marker_missing" if missing else "success_marker_invalid")
    test = value["restore"]["latest"]
    result.update(latest_restore=test, restore_collection_status=value["restore"]["status"])
    if value["restore"]["status"] == "invalid" or latest is None:
        return result
    result["restoration_state"] = "not_tested"
    if test is not None:
        matches = test["snapshot"] == latest["snapshot"]
        result["same_snapshot"] = matches
        if matches:
            consistent = (test["captured_at"] == latest["frozen_at_utc"] and
                test["tables_verified"] == latest["tables"] and test["rows_verified"] == latest["rows"])
            state = test["status"]
            result["restoration_state"] = ("validated" if consistent else "unknown") if state == "restore_validated" else state
    result["verification"]["restoration"] = result["restoration_state"]
    return result
