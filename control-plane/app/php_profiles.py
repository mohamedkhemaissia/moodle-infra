"""Lecture du dernier resultat enregistre ; aucune ecriture de configuration PHP."""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def strict(pairs):
    result = {}
    for key, value in pairs:
        if key in result: raise ValueError("Cle dupliquee.")
        result[key] = value
    return result


def read_status(directory, now):
    result = {"status": "unavailable", "evaluated_at": iso(now), "mode": "read_only",
              "reason": "profiles_not_prepared", "recorded_profile": None, "profiles": []}
    if not directory: return result
    root = Path(directory) / "php-profiles"
    try:
        fd = os.open(root / "status.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096: raise ValueError("Fichier incompatible.")
            body = os.read(fd, 4097)
        finally:
            os.close(fd)
        value = json.loads(body, object_pairs_hook=strict,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nombre invalide.")))
        if (value["format"] != "php-fpm-profile-status-v1" or len(value["profiles"]) != 2
                or value["state"] not in ("prepared", "changing", "verified", "rolled_back", "review_required")
                or value["recorded_profile"] not in ("normal", "burst", None)
                or type(value["activation_revision"]) is not int or value["activation_revision"] < 1
                or type(value["recorded_at"]) not in (int, float) or not math.isfinite(value["recorded_at"])
                or not 0 <= value["recorded_at"] <= now
                or value["run_id"] is not None and not re.fullmatch(r"[a-f0-9]{32}", value["run_id"])):
            raise ValueError("Etat incompatible.")
        profiles = {}
        for p in value["profiles"]:
            if (p["id"] not in ("normal", "burst") or p["id"] in profiles
                    or type(p["max_children"]) is not int
                    or p["max_children"] != {"normal": 5, "burst": 8}[p["id"]]
                    or not re.fullmatch(r"[a-f0-9]{64}", p["sha256"])):
                raise ValueError("Profil incompatible.")
            profiles[p["id"]] = {key: p[key] for key in ("id", "max_children", "sha256")}
        pending = (root / "pending.json").exists()
        state = value["state"]
        if pending and (state != "changing" or now - value["recorded_at"] > 90): state = "review_required"
        return {"status": "available", "mode": "read_only", "evaluated_at": iso(now),
                "recorded_at": iso(value["recorded_at"]), "state": state,
                "recorded_profile": value["recorded_profile"], "profiles": list(profiles.values()),
                "activation_revision": value["activation_revision"], "run_id": value["run_id"],
                "review_required": state == "review_required",
                "semantics": {"record": "Dernier resultat enregistre ; consulter les mesures pour l etat present.",
                    "action": "File confirmee, ressources suffisantes et autorisation de politique avant toute modification.",
                    "activation": "Une politique plus recente que la preparation des profils est requise."}}
    except FileNotFoundError:
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, OverflowError):
        return {**result, "reason": "profile_record_unavailable"}
