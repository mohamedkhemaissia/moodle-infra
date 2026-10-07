"""Revalidation d'une recommandation recente avant un diagnostic autorise."""
import math
import re
from datetime import datetime

POLICY_SHA = "96246d2d412bdf0b6ff739ea558e18e0ff1107c3d2cf87248ac785429d1ac529"
ACTIONS = {
    "inspect_prometheus": "collection", "inspect_linux_collection": "collection",
    "inspect_moodle_collection": "collection", "wait_contiguous_history": "collection",
    "inspect_anomaly_model": "model", "diagnose_moodle_https": "moodle",
    "diagnose_disk_usage": "resources", "diagnose_memory_usage": "resources",
    "diagnose_cpu_usage": "resources", "investigate_statistical_anomaly": "resources",
}


class Blocked(Exception):
    pass


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Date invalide.")
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError("Fuseau horaire absent.")
    return date.timestamp()


def validate(document, incident_id, now, *, expected=None):
    try:
        d = document["decision"]
        action = d["recommended_action"]
        if document["status"] != "available" or document["mode"] != "simulation" or document["snapshot_usable"] is not True:
            raise ValueError("snapshot_unusable")
        if d["incident_id"] != incident_id or type(d["incident_id"]) is not int or \
                d["incident_status"] != "open" or d["latest_state"] != "triggered" or d["status"] != "recommendation":
            raise ValueError("no_current_recommendation")
        if action["kind"] != "diagnostic_recommendation" or action["executable"] is not False or action["id"] not in ACTIONS:
            raise ValueError("unsupported_action")
        if document["policy_sha256"] != POLICY_SHA or d["policy_sha256"] != POLICY_SHA or d["policy_version"] != "1.0.0":
            raise ValueError("unsupported_policy")
        start, expiry = timestamp(d["input_evaluated_at"]), timestamp(d["valid_until"])
        if not math.isfinite(now) or start > now or expiry <= now or not 0 < expiry - start <= 90 or now - start > 90:
            raise ValueError("expired_or_inconsistent_decision")
        if not re.fullmatch(r"[0-9a-f]{64}", d["decision_id"]):
            raise ValueError("invalid_decision_id")
        result = {"incident_id": incident_id, "action_id": action["id"], "profile": ACTIONS[action["id"]],
                  "decision_id": d["decision_id"], "policy_sha256": POLICY_SHA, "policy_version": "1.0.0",
                  "context": d["context"], "input_evaluated_at": d["input_evaluated_at"], "valid_until": d["valid_until"]}
        if expected and any(result[key] != expected[key] for key in ("incident_id", "action_id", "policy_sha256", "context")):
            raise ValueError("recommendation_changed")
        return result
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise Blocked("Aucune recommandation recente compatible : " + str(exc)) from exc
