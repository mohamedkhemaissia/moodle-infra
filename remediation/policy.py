"""Conditions necessaires pour proposer l'action PHP du premier perimetre."""
import math

from diagnostics.policy import Blocked, timestamp, validate
from .store import ACTION

PROFILE_ACTION = "apply_php_fpm_profile"


def decision_for_php(document, incident_id, now, *, expected=None):
    try:
        current = validate(document, incident_id, now, expected=expected)
        decision = document["decision"]
        if decision["signal_key"] != "moodle_https" or current["action_id"] != "diagnose_moodle_https" or \
                decision["evidence"].get("probe_success") is not False:
            raise Blocked("unsupported_incident")
        current["correction_action"] = ACTION
        current["execution_mode"] = "manual_guarded"
        return current
    except (KeyError, TypeError, ValueError) as exc:
        raise Blocked("invalid_moodle_evidence") from exc


def decision_for_correction(document, incident_id, now, *, expected=None):
    if document.get("decision", {}).get("signal_key") == "moodle_https":
        return decision_for_php(document, incident_id, now, expected=expected)
    current = validate(document, incident_id, now, expected=expected)
    decision = document["decision"]
    evidence = decision.get("evidence", {})
    if (decision.get("signal_key") != "php_pool_queue" or current["action_id"] != "diagnose_php_pool"
            or evidence.get("pool") != "www" or evidence.get("idle_processes") != 0
            or type(evidence.get("listen_queue")) not in (int, float) or not 0 < evidence["listen_queue"] <= 100000
            or type(evidence.get("active_processes")) not in (int, float) or not 0 < evidence["active_processes"] <= 100000
            or evidence.get("active_processes") != evidence.get("total_processes")):
        raise Blocked("unsupported_pool_incident")
    current.update(correction_action=PROFILE_ACTION, execution_mode="manual_guarded")
    return current


def metric_gate(document, now, *, available):
    try:
        if document["source"] != "prometheus" or not 0 <= now - timestamp(document["evaluated_at"]) <= 15:
            raise ValueError("stale_metrics")
        for job in ("linux", "moodle_https"):
            source = document["collection"][job]
            age = source["scrape_age_seconds"]
            if source["status"] != "up" or type(age) not in (int,float) or not math.isfinite(age) or not 0 <= age <= 60:
                raise ValueError("unhealthy_collection")
        metrics = document["metrics"]
        if metrics["moodle_available"] is not available:
            raise ValueError("probe_not_in_expected_state")
        for key in ("cpu_usage_percent", "memory_usage_percent", "disk_available_percent"):
            value = metrics[key]
            if type(value) not in (int,float) or not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError("invalid_resource_metrics")
        if not available and (metrics["cpu_usage_percent"] >= 85 or metrics["memory_usage_percent"] >= 90 or metrics["disk_available_percent"] <= 10):
            raise ValueError("resource_pressure")
        return timestamp(document["evaluated_at"]) - document["collection"]["moodle_https"]["scrape_age_seconds"]
    except (KeyError, TypeError, ValueError) as exc:
        raise Blocked(str(exc)) from exc
