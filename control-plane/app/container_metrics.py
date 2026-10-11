"""Inventaire Docker et mesures textfile ; aucune commande de correction."""
import asyncio
import json
import math
import re

import httpx

from .prometheus import PrometheusInvalidResponse, PrometheusUnavailable

METRICS = ("cpu_usage_percent", "memory_working_set_bytes", "memory_limit_bytes", "pids")
FIELDS = ("instances", "present", "running", "paused", "health", "stats_success", "stats_timestamp_seconds") + METRICS
MAX_COLLECTION_AGE = 90
DEFAULT_TARGETS = [{"project": "moodle-pilote", "service": s} for s in ("apache", "php", "postgres")]


def parse_targets(value):
    targets = json.loads(value) if isinstance(value, str) else value
    if not isinstance(targets, list) or not 1 <= len(targets) <= 24:
        raise ValueError("Liste de conteneurs invalide.")
    result = []
    for item in targets:
        if (not isinstance(item, dict) or set(item) != {"project", "service"}
                or any(not isinstance(item[k], str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", item[k])
                       for k in ("project", "service"))):
            raise ValueError("Identite de conteneur invalide.")
        key = (item["project"], item["service"])
        if key in result:
            raise ValueError("Service duplique.")
        result.append(key)
    return result


def queries():
    return {**{name: 'moodle_container_' + name + '{job="linux",compose_service=~".+"}' for name in FIELDS},
            "collection_success": 'moodle_container_collection_success{job="linux"}',
            "inventory_timestamp_seconds": 'moodle_container_inventory_timestamp_seconds{job="linux"}',
            "textfile_error": 'node_textfile_scrape_error{job="linux"}',
            "linux_up": 'up{job="linux"}', "linux_age": 'time() - timestamp(up{job="linux"})'}


async def vector(prometheus, expression, evaluated_at, targets):
    try:
        response = await prometheus.client.get("api/v1/query", params={"query": expression, "time": evaluated_at.timestamp()})
    except httpx.RequestError as exc:
        raise PrometheusUnavailable("Prometheus indisponible.") from exc
    if response.status_code >= 500:
        raise PrometheusUnavailable("Prometheus indisponible.")
    try:
        document = response.json()
        data = document["data"]
        if (response.status_code != 200 or document["status"] != "success" or data["resultType"] != "vector"
                or not isinstance(data["result"], list) or len(data["result"]) > 100):
            raise ValueError("Vecteur invalide.")
        result = {}
        for item in data["result"]:
            key = (item["metric"]["project"], item["metric"]["compose_service"])
            if key not in targets:
                continue
            sample = item["value"]
            if key in result or not isinstance(sample, list) or len(sample) != 2 or not isinstance(sample[1], str):
                raise ValueError("Mesure ambigue.")
            value = float(sample[1])
            if math.isfinite(value) and value < 0:
                raise ValueError("Mesure negative.")
            result[key] = value if math.isfinite(value) else None
        return result
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise PrometheusInvalidResponse("Mesures de conteneurs inexploitables.") from exc


def reading(key, values, collector, now):
    fields = {name: values[name].get(key) for name in FIELDS}
    instances, present = fields["instances"], fields["present"]
    for name in ("present", "running", "paused", "stats_success"):
        if fields[name] not in (None, 0, 1):
            raise PrometheusInvalidResponse("Etat Docker invalide.")
    if fields["health"] not in (None, 0, 1, 2) or (instances is not None and (instances != int(instances) or instances > 1000)):
        raise PrometheusInvalidResponse("Inventaire Docker invalide.")
    if present is not None and instances is not None and bool(present) != (instances > 0):
        raise PrometheusInvalidResponse("Inventaire Docker incoherent.")
    state, health = "unknown", None
    fresh = collector["status"] == "up"
    if fresh and instances is not None and present is not None:
        state = "missing" if instances == 0 else "ambiguous" if instances > 1 else "unknown"
        if instances == 1 and fields["running"] is not None and fields["paused"] is not None:
            if fields["running"] == 1 and fields["paused"] == 1:
                raise PrometheusInvalidResponse("Etat Docker incoherent.")
            state = "running" if fields["running"] == 1 else "paused" if fields["paused"] == 1 else "not_running"
            if state == "running" and fields["health"] is not None:
                health = {0: "unhealthy", 1: "healthy", 2: "starting"}[fields["health"]]
    stamp = fields["stats_timestamp_seconds"]
    stats_age = None if stamp is None else now - stamp
    usable = (fresh and state == "running" and fields["stats_success"] == 1
              and stats_age is not None and 0 <= stats_age <= MAX_COLLECTION_AGE)
    metrics = {name: round(fields[name], 3) if usable and fields[name] is not None else None for name in METRICS}
    metrics_status = "available" if all(v is not None for v in metrics.values()) else "partial" if any(v is not None for v in metrics.values()) else "unavailable"
    return {"project": key[0], "service": key[1], "state": state, "health": health,
            "instances": int(instances) if fresh and instances is not None else None,
            "metrics_status": metrics_status, "stats_age_seconds": stats_age if stats_age is not None and stats_age >= 0 else None,
            "metrics": metrics}


async def summary(prometheus, evaluated_at, targets):
    expressions = queries()
    try:
        results = await asyncio.wait_for(asyncio.gather(*(
            vector(prometheus, expression, evaluated_at, targets) if name in FIELDS else
            prometheus.query(expression, evaluated_at.timestamp())
            for name, expression in expressions.items()), return_exceptions=True), timeout=6)
    except asyncio.TimeoutError as exc:
        raise PrometheusUnavailable("Delai de lecture des conteneurs depasse.") from exc
    for result in results:
        if isinstance(result, BaseException):
            raise result
    values = dict(zip(expressions, results))
    linux = prometheus.source_status(values["linux_up"], values["linux_age"])
    stamp = values["inventory_timestamp_seconds"]
    inventory_age = None if stamp is None else evaluated_at.timestamp() - stamp
    if values["collection_success"] not in (None, 0, 1) or values["textfile_error"] not in (None, 0, 1):
        raise PrometheusInvalidResponse("Etat de collecte invalide.")
    if linux.status != "up":
        status = linux.status
    elif values["collection_success"] == 0 or values["textfile_error"] == 1:
        status = "down"
    elif (values["collection_success"] is None or values["textfile_error"] is None or inventory_age is None or inventory_age < 0):
        status = "missing"
    else:
        status = "up" if inventory_age <= MAX_COLLECTION_AGE else "stale"
    collector = {"status": status, "age_seconds": inventory_age if inventory_age is not None and inventory_age >= 0 else None}
    containers = [reading(key, values, collector, evaluated_at.timestamp()) for key in targets]
    available = [c["state"] not in ("unknown", "ambiguous") and (c["state"] != "running" or c["metrics_status"] == "available") for c in containers]
    return {"status": "available" if all(available) else "partial" if any(c['state'] != 'unknown' for c in containers) else "unavailable",
            "source": "prometheus", "mode": "read_only", "collection": linux.model_dump(), "collector": collector,
            "evaluated_at": evaluated_at.isoformat().replace("+00:00", "Z"),
            "max_scrape_age_seconds": prometheus.max_scrape_age_seconds, "max_collection_age_seconds": MAX_COLLECTION_AGE,
            "containers": containers, "semantics": {
                "cpu": "100 % correspond a un coeur utilise ; la valeur peut depasser 100 %.",
                "memory": "Usage Docker moins le cache inactif, comme docker stats sous Linux.",
                "limit": "Sans limite explicite, la limite Docker effective peut correspondre a la RAM de l'hote.",
                "state": "Etat Docker ; ne remplace pas les controles PHP, PostgreSQL et HTTPS Moodle."}}
