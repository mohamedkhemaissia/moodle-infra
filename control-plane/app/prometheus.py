import asyncio
import math
from datetime import datetime

import httpx
from pydantic import ValidationError

from .models import Metrics, MetricsSummary, SourceStatus


METRIC_QUERIES = {
    "cpu_usage_percent": (
        '100 * (1 - avg(rate(node_cpu_seconds_total{job="linux",mode="idle"}[2m])))'
    ),
    "memory_usage_percent": (
        '100 * (1 - node_memory_MemAvailable_bytes{job="linux"}'
        ' / node_memory_MemTotal_bytes{job="linux"})'
    ),
    "disk_available_percent": (
        '100 * node_filesystem_avail_bytes{job="linux",mountpoint="/",fstype!~"tmpfs|overlay"}'
        ' / node_filesystem_size_bytes{job="linux",mountpoint="/",fstype!~"tmpfs|overlay"}'
    ),
    "moodle_available": 'probe_success{job="moodle_https"}',
    "moodle_probe_duration_ms": 'probe_duration_seconds{job="moodle_https"} * 1000',
    "moodle_http_status_code": 'probe_http_status_code{job="moodle_https"}',
}

METRIC_SOURCES = {
    name: "moodle_https" if name.startswith("moodle_") else "linux"
    for name in METRIC_QUERIES
}


class PrometheusUnavailable(Exception):
    pass


class PrometheusInvalidResponse(Exception):
    pass


class PrometheusClient:
    def __init__(self, client: httpx.AsyncClient, max_scrape_age_seconds: float = 60):
        if not math.isfinite(max_scrape_age_seconds) or max_scrape_age_seconds <= 0:
            raise ValueError("Le delai de fraicheur doit etre positif et fini.")
        self.client = client
        self.max_scrape_age_seconds = max_scrape_age_seconds

    async def query(self, expression: str, evaluation_time: float) -> float | None:
        try:
            response = await self.client.get(
                "api/v1/query", params={"query": expression, "time": evaluation_time}
            )
        except httpx.RequestError as exc:
            raise PrometheusUnavailable("Connexion Prometheus indisponible.") from exc
        if response.status_code >= 500:
            raise PrometheusUnavailable("Erreur serveur Prometheus.")
        if response.status_code != 200:
            raise PrometheusInvalidResponse("Statut HTTP Prometheus inattendu.")
        try:
            payload = response.json()
            if payload["status"] != "success":
                raise PrometheusInvalidResponse("Echec de la requete Prometheus.")
            if payload["data"]["resultType"] != "vector":
                raise PrometheusInvalidResponse("Type de resultat inattendu.")
            results = payload["data"]["result"]
            if not isinstance(results, list) or len(results) > 1:
                raise PrometheusInvalidResponse("Resultat ambigu pour le pilote unique.")
            if not results:
                return None
            sample = results[0]["value"]
            if not isinstance(sample, list) or len(sample) != 2 or not isinstance(sample[1], str):
                raise PrometheusInvalidResponse("Echantillon Prometheus invalide.")
            value = float(sample[1])
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise PrometheusInvalidResponse("Reponse Prometheus invalide.") from exc
        return value if math.isfinite(value) else None

    def source_status(self, up: float | None, age: float | None) -> SourceStatus:
        if up not in (0.0, 1.0) or age is None or age < 0:
            status = "missing"
        elif age > self.max_scrape_age_seconds:
            status = "stale"
        else:
            status = "up" if up == 1 else "down"
        return SourceStatus(status=status, scrape_age_seconds=age)

    async def summary(self, evaluated_at: datetime) -> MetricsSummary:
        queries = dict(METRIC_QUERIES)
        for job in ("linux", "moodle_https"):
            queries[f"{job}_up"] = f'up{{job="{job}"}}'
            queries[f"{job}_age"] = f'time() - timestamp(up{{job="{job}"}})'
        try:
            results = await asyncio.wait_for(
                asyncio.gather(
                    *(self.query(expr, evaluated_at.timestamp()) for expr in queries.values()),
                    return_exceptions=True,
                ),
                timeout=6,
            )
        except asyncio.TimeoutError as exc:
            raise PrometheusUnavailable("Delai global Prometheus depasse.") from exc
        for result in results:
            if isinstance(result, PrometheusUnavailable):
                raise result
        for result in results:
            if isinstance(result, Exception):
                raise result
        values = dict(zip(queries, results))
        collection = {
            job: self.source_status(values[f"{job}_up"], values[f"{job}_age"])
            for job in ("linux", "moodle_https")
        }
        metrics = {
            name: round(values[name], 3)
            if values[name] is not None and collection[METRIC_SOURCES[name]].status == "up"
            else None
            for name in METRIC_QUERIES
        }
        try:
            readings = Metrics(**metrics)
        except ValidationError as exc:
            raise PrometheusInvalidResponse("Valeurs metriques invalides.") from exc
        return MetricsSummary(
            evaluated_at=evaluated_at,
            collection=collection,
            metrics=readings,
            unavailable_metrics=[name for name, value in metrics.items() if value is None],
        )
