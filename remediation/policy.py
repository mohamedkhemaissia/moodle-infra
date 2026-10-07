"""Conditions necessaires pour proposer l'action PHP du premier perimetre."""
import math

from diagnostics.policy import Blocked, timestamp, validate
from .store import ACTION


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
