"""Catalogue public de references Restic ; dates des donnees distinctes de l'archivage."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat

if __package__ == "backup_status":
    from . import status as common
else:
    from . import backup_status as common

SCOPES = ("moodle", "pilotage")
MAX_ITEMS = 200
MAX_BYTES = 262144
MAX_AGE_SECONDS = 900
REASONS = {"repository_unavailable", "repository_timeout", "repository_output_too_large",
           "repository_output_invalid", "scope_metadata_invalid", "not_configured"}
HOST = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,253}")


def integer(value, maximum=10000):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("Comptage invalide.")
    return value


def unavailable(reason):
    if reason not in REASONS:
        raise ValueError("Motif invalide.")
    return {"status": "unavailable", "reason": reason, "total": None, "items": [],
            "truncated": False, "latest_success_presence": "unknown", "latest_success_id": None}


def validate_scope(value, observed):
    if not isinstance(value, dict) or set(value) != {
            "status", "reason", "total", "items", "truncated", "latest_success_presence", "latest_success_id"}:
        raise ValueError("Perimetre invalide.")
    if value["status"] == "unavailable":
        if value != unavailable(value["reason"]):
            raise ValueError("Etat indisponible incoherent.")
        return value
    if value["status"] != "available" or value["reason"] is not None:
        raise ValueError("Etat invalide.")
    total = integer(value["total"])
    items = value["items"]
    if not isinstance(items, list) or len(items) != min(total, MAX_ITEMS):
        raise ValueError("Catalogue incomplet.")
    if type(value["truncated"]) is not bool or value["truncated"] != (total > MAX_ITEMS):
        raise ValueError("Limite incoherente.")
    latest = value["latest_success_id"]
    presence = value["latest_success_presence"]
    if presence not in ("present", "absent", "unknown"):
        raise ValueError("Confirmation invalide.")
    if latest is None:
        if presence != "unknown":
            raise ValueError("Reference absente.")
    elif not isinstance(latest, str) or not common.SNAPSHOT.fullmatch(latest) or presence == "unknown":
        raise ValueError("Reference invalide.")
    identifiers, order = set(), []
    for item in items:
        if not isinstance(item, dict) or set(item) != {
                "id", "archived_at", "recovery_at", "hostname", "success_recorded"}:
            raise ValueError("Point invalide.")
        if not isinstance(item["id"], str) or not common.SNAPSHOT.fullmatch(item["id"]) or item["id"] in identifiers:
            raise ValueError("Reference invalide ou dupliquee.")
        if not isinstance(item["hostname"], str) or not HOST.fullmatch(item["hostname"]):
            raise ValueError("Hote invalide.")
        archived = common.utc(item["archived_at"])
        if archived > observed or type(item["success_recorded"]) is not bool:
            raise ValueError("Date ou confirmation invalide.")
        confirmed = item["id"] == latest and presence == "present"
        if item["success_recorded"] != confirmed:
            raise ValueError("Rapprochement invalide.")
        recovery = item["recovery_at"]
        if confirmed:
            if recovery is None or common.utc(recovery) > archived:
                raise ValueError("Instant des donnees invalide.")
        elif recovery is not None:
            raise ValueError("Instant des donnees sans preuve.")
        identifiers.add(item["id"])
        order.append((item["archived_at"], item["id"]))
    if order != sorted(order, reverse=True):
        raise ValueError("Ordre incoherent.")
    if latest in identifiers and presence != "present":
        raise ValueError("Presence incoherente.")
    if presence == "present" and latest not in identifiers and not value["truncated"]:
        raise ValueError("Point confirme absent du catalogue.")
    return value


def validate_document(value):
    if not isinstance(value, dict) or set(value) != {"schema_version", "observed_at", "scopes"}:
        raise ValueError("Catalogue incompatible.")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Version incompatible.")
    observed = common.utc(value["observed_at"])
    if not isinstance(value["scopes"], dict) or set(value["scopes"]) != set(SCOPES):
        raise ValueError("Perimetres incompatibles.")
    for item in value["scopes"].values():
        validate_scope(item, observed)
    return value


def read_document(path):
    path = Path(path)
    if any(parent.is_symlink() for parent in path.parents):
        raise ValueError("Repertoire lie.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
            raise ValueError("Fichier inexploitable.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Fichier trop grand.")
        return validate_document(json.loads(raw, object_pairs_hook=common._pairs, parse_constant=common._constant))
    finally:
        os.close(descriptor)


def summary(path, now: datetime, scope="moodle", limit=20, offset=0):
    if scope not in SCOPES or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("Selection invalide.")
    integer(offset, MAX_ITEMS)
    result = {"status": "unavailable", "mode": "read_only", "scope": scope,
        "evaluated_at": common.encoded_time(now), "reason": "catalog_file_missing",
        "collection": {"status": "missing", "observed_at": None, "age_seconds": None,
                       "max_age_seconds": MAX_AGE_SECONDS},
        "items": [], "total": None, "listed_total": 0, "truncated": False,
        "limit": limit, "offset": offset, "has_more": False,
        "latest_success_presence": "unknown", "latest_success_id": None,
        "verification": {"repository_integrity": "not_checked", "restoration": "not_checked"}}
    try:
        document = read_document(path)
    except FileNotFoundError:
        return result
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
        result.update(reason="catalog_file_invalid")
        result["collection"]["status"] = "invalid"
        return result
    age = (now - common.utc(document["observed_at"])).total_seconds()
    result["collection"].update(observed_at=document["observed_at"], age_seconds=round(age, 3))
    if age < 0 or age > MAX_AGE_SECONDS:
        result["reason"] = "clock_inconsistent" if age < 0 else "collection_stale"
        result["collection"]["status"] = "invalid" if age < 0 else "stale"
        return result
    value = document["scopes"][scope]
    result["collection"]["status"] = "up"
    if value["status"] != "available":
        result["reason"] = value["reason"]
        return result
    items = value["items"]
    result.update(status="available", reason=None, items=items[offset:offset+limit], total=value["total"],
                  listed_total=len(items), truncated=value["truncated"], has_more=offset+limit < len(items),
                  latest_success_presence=value["latest_success_presence"], latest_success_id=value["latest_success_id"])
    return result
