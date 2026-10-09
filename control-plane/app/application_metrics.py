"""Indicateurs applicatifs en lecture seule ; collecte et acces au service distincts."""
import asyncio
import math
import re

from .prometheus import PrometheusInvalidResponse, PrometheusUnavailable


PHP_JOB = "moodle_phpfpm"
PG_JOB = "moodle_postgres"
PHP_KEYS = ("active_processes", "idle_processes", "total_processes", "listen_queue", "max_children_reached")
PG_KEYS = ("database_connections", "server_connections", "max_connections", "commits_per_second", "rollbacks_per_second", "deadlocks_total")


def queries(pool="www", database="moodle"):
    if not re.fullmatch(r"[a-zA-Z_][a-zA-Z_0-9-]{0,62}", pool) or not re.fullmatch(r"[a-z_][a-z_0-9]{0,62}", database):
        raise ValueError("Identite du pool ou de la base invalide.")
    php = f'job="{PHP_JOB}",pool="{pool}"'
    pg = f'job="{PG_JOB}",datname="{database}"'
    result = {"php." + key: f"phpfpm_{key}{{{php}}}" for key in PHP_KEYS}
    result.update({
        "pg.database_connections": f"pg_stat_database_numbackends{{{pg}}}",
        "pg.server_connections": f'sum(pg_stat_database_numbackends{{job="{PG_JOB}"}})',
        "pg.max_connections": f'pg_settings_max_connections{{job="{PG_JOB}"}}',
        "pg.commits_per_second": f"rate(pg_stat_database_xact_commit{{{pg}}}[2m])",
        "pg.rollbacks_per_second": f"rate(pg_stat_database_xact_rollback{{{pg}}}[2m])",
        "pg.deadlocks_total": f"pg_stat_database_deadlocks{{{pg}}}",
        "php.service_up": f'phpfpm_up{{job="{PHP_JOB}"}}',
        "pg.service_up": f'pg_up{{job="{PG_JOB}"}}',
    })
    for prefix, job in (("php", PHP_JOB), ("pg", PG_JOB)):
        result[prefix + ".up"] = f'up{{job="{job}"}}'
        result[prefix + ".age"] = f'time() - timestamp(up{{job="{job}"}})'
        # Refuser aussi un ancien indicateur applicatif apres un changement de labels.
        result[prefix + ".service_age"] = 'time() - timestamp(' + result[prefix + ".service_up"] + ')'
    return result


def reading(prefix, keys, values, prometheus):
    collection = prometheus.source_status(values[prefix + ".up"], values[prefix + ".age"])
    service_up, service_age = values[prefix + ".service_up"], values[prefix + ".service_age"]
    if service_up not in (None, 0, 1):
        raise PrometheusInvalidResponse("Etat applicatif invalide.")
    fresh = collection.status == "up" and service_age is not None and 0 <= service_age <= prometheus.max_scrape_age_seconds
    reachable = service_up == 1 if fresh and service_up is not None else None
    metrics = {}
    for key in keys:
        value = values[prefix + "." + key]
        if value is not None and (not math.isfinite(value) or value < 0):
            raise PrometheusInvalidResponse("Indicateur applicatif invalide.")
        metrics[key] = round(value, 3) if fresh and reachable is True and value is not None else None
    missing = [key for key, value in metrics.items() if value is None]
    status = "available" if not missing else "partial" if any(v is not None for v in metrics.values()) else "unavailable"
    return {"status": status, "collection": collection.model_dump(), "service_reachable": reachable,
            "metrics": metrics, "unavailable_metrics": missing}


async def summary(prometheus, evaluated_at, pool="www", database="moodle"):
    expressions = queries(pool, database)
    try:
        results = await asyncio.wait_for(asyncio.gather(
            *(prometheus.query(query, evaluated_at.timestamp()) for query in expressions.values()),
            return_exceptions=True), timeout=6)
    except asyncio.TimeoutError as exc:
        raise PrometheusUnavailable("Delai de lecture applicative depasse.") from exc
    for result in results:
        if isinstance(result, PrometheusUnavailable):
            raise result
    for result in results:
        if isinstance(result, BaseException):
            raise result
    values = dict(zip(expressions, results))
    services = {"php_fpm": reading("php", PHP_KEYS, values, prometheus),
                "postgresql": reading("pg", PG_KEYS, values, prometheus)}
    available = [service["status"] == "available" for service in services.values()]
    overall = "available" if all(available) else "unavailable" if all(s["status"] == "unavailable" for s in services.values()) else "partial"
    return {"status": overall, "source": "prometheus", "mode": "read_only",
            "evaluated_at": evaluated_at.isoformat().replace("+00:00", "Z"),
            "max_scrape_age_seconds": prometheus.max_scrape_age_seconds,
            "services": services,
            "semantics": {"reachability": "Acces depuis l'exporteur ; ne remplace pas le controle HTTPS Moodle.",
                          "counters": "Compteurs depuis le demarrage du service ou la remise a zero des statistiques.",
                          "rates": "Transactions par seconde, calculees sur deux minutes.",
                          "action": "Lecture seule ; aucune correction declenchee."}}
