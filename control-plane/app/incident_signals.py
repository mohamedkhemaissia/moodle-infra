"""Traduire les mesures en signaux independants, avec valeurs inconnues explicites."""
import math

from .incidents import DEFINITIONS, Observation

RESOURCE_RULES = {
    "cpu_high": ("cpu_usage_percent",85.0,75.0,"upper"),
    "memory_high": ("memory_usage_percent",90.0,85.0,"upper"),
    "disk_low": ("disk_available_percent",10.0,15.0,"lower"),
}


def observations(metrics, anomaly, *, prometheus_error=None, anomaly_error=None, model_enabled=True, applications=None):
    result = {key:Observation(key,"unknown",{"reason":"observation_unavailable"}) for key in DEFINITIONS}
    result["prometheus_unavailable"] = Observation("prometheus_unavailable",
        "triggered" if prometheus_error else "clear", {"reason":prometheus_error or "prometheus_read_successful"})
    result["anomaly_model"] = Observation("anomaly_model",
        "triggered" if model_enabled and anomaly_error == "model_unavailable" else "clear",
        {"configured":model_enabled,"reason":anomaly_error or ("model_loaded" if model_enabled else "analysis_disabled")})
    if metrics is not None:
        data = metrics.model_dump(mode="json") if hasattr(metrics,"model_dump") else metrics
        for key,job in (("linux_collection","linux"),("moodle_collection","moodle_https")):
            source = data["collection"][job]
            result[key] = Observation(key,"clear" if source["status"] == "up" else "triggered",
                {"job":job,"collection_status":source["status"],"scrape_age_seconds":source["scrape_age_seconds"]})
        values = data["metrics"]
        def valid_percent(value):
            return type(value) in (int,float) and math.isfinite(value) and 0 <= value <= 100
        bad_resources = [metric for metric in ("cpu_usage_percent","memory_usage_percent","disk_available_percent")
                         if not valid_percent(values[metric])]
        if data["collection"]["linux"]["status"] == "up" and bad_resources:
            result["linux_collection"] = Observation("linux_collection","triggered",
                {"job":"linux","collection_status":"up","reason":"incomplete_resource_metrics","invalid_metrics":bad_resources})
        moodle_known = data["collection"]["moodle_https"]["status"] == "up" and type(values["moodle_available"]) is bool
        if not moodle_known and data["collection"]["moodle_https"]["status"] == "up":
            result["moodle_collection"] = Observation("moodle_collection","triggered",
                {"job":"moodle_https","collection_status":"up","reason":"incomplete_probe_metrics"})
        if moodle_known:
            result["moodle_https"] = Observation("moodle_https","clear" if values["moodle_available"] else "triggered",
                {"probe_success":values["moodle_available"],"http_status_code":values["moodle_http_status_code"],
                 "probe_duration_ms":values["moodle_probe_duration_ms"],"scope":"sonde HTTPS de la page de connexion"})
        for key,(metric,trigger,recovery,direction) in RESOURCE_RULES.items():
            value = values[metric]
            evidence = {"metric":metric,"value":value,"trigger_threshold":trigger,"recovery_threshold":recovery,
                        "direction":direction,"scope":"ressource serveur ; seuil experimental"}
            if data["collection"]["linux"]["status"] != "up" or not valid_percent(value):
                state = "unknown"
                evidence["reason"] = "resource_collection_unavailable" if value is None else "invalid_or_unhealthy_resource_observation"
                if type(value) not in (int,float) or not math.isfinite(value):
                    evidence["value"] = None
            elif direction == "upper":
                state = "triggered" if value >= trigger else "clear" if value <= recovery else "hold"
            else:
                state = "triggered" if value <= trigger else "clear" if value >= recovery else "hold"
            result[key] = Observation(key,state,evidence)
    if not model_enabled:
        result["analysis_history"] = Observation("analysis_history","clear",{"reason":"analysis_disabled"})
    elif anomaly is not None:
        identifier = anomaly.get("model_id", "unconfigured")
        context = "model:"+identifier
        if anomaly["status"] == "available":
            result["analysis_history"] = Observation("analysis_history","clear",{"reason":"contiguous_history_available"},context)
            score,threshold = anomaly["anomaly"]["score"],anomaly["anomaly"]["threshold"]
            recovery = max(0.0,threshold-0.02)
            state = "triggered" if score > threshold else "clear" if score <= recovery else "hold"
            result["statistical_anomaly"] = Observation("statistical_anomaly",state,
                {"score":score,"threshold":threshold,"recovery_threshold":recovery,"model_id":identifier,
                 "input_evaluated_at":anomaly["input_evaluated_at"],"experimental":True,
                 "quality":"not_evaluated_no_incident_labels","scope":"observation atypique ; aucune cause de panne determinee"},context)
        else:
            result["analysis_history"] = Observation("analysis_history","triggered",
                {"reason":anomaly["reason"],"required_history_seconds":anomaly["required_history_seconds"],
                 "invalid_point_count":len(anomaly.get("invalid_points",[])),"model_id":identifier},context)
            result["statistical_anomaly"] = Observation("statistical_anomaly","unknown",
                {"reason":anomaly["reason"],"model_id":identifier},context)
    if applications is not None:
        try:
            php = applications["services"]["php_fpm"]
            age = php["collection"]["scrape_age_seconds"]
            values = php["metrics"]
            keys = ("active_processes", "idle_processes", "total_processes", "listen_queue")
            good = (php["status"] == "available" and php["service_reachable"] is True
                    and php["collection"]["status"] == "up" and type(age) in (int, float) and 0 <= age <= 30
                    and all(type(values[k]) in (int, float) and math.isfinite(values[k])
                            and 0 <= values[k] <= 100000 and values[k] == int(values[k]) for k in keys)
                    and values["total_processes"] == values["active_processes"] + values["idle_processes"])
            if good:
                busy = values["listen_queue"] > 0 and values["idle_processes"] == 0 and values["active_processes"] > 0
                state = "triggered" if busy else "clear" if values["listen_queue"] == 0 else "hold"
                result["php_pool_queue"] = Observation("php_pool_queue", state,
                    {"pool": "www", **{k: values[k] for k in keys}, "scrape_age_seconds": age,
                     "scope": "file actuelle du pool ; ne determine pas une cause"})
        except (KeyError, TypeError, ValueError):
            pass
    return list(result.values())
