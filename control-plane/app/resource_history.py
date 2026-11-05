"""Historique des ressources utilise par la detection d'anomalies."""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math

import httpx

from .prometheus import METRIC_QUERIES, PrometheusInvalidResponse, PrometheusUnavailable

METRICS = ("cpu_usage_percent", "memory_usage_percent", "disk_available_percent")
STATISTICS = ("current", "mean", "std", "min", "max", "delta", "slope_per_minute")
FEATURE_NAMES = [f"{metric}__{stat}" for metric in METRICS for stat in STATISTICS]
MAX_JSON_BYTES = 2 * 1024 * 1024


def finite_number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Nombre fini attendu.")
    return float(value)


def utc_iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Cle JSON repetee.")
            result[key] = value
        return result

    def reject(value):
        raise ValueError(f"Nombre JSON invalide : {value}")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=reject)


def window_statistics(values, step):
    count = len(values)
    average = math.fsum(values) / count
    variance = math.fsum((value - average) ** 2 for value in values) / count
    center = (count - 1) / 2
    denominator = math.fsum(((index - center) * step / 60) ** 2 for index in range(count))
    slope = math.fsum((index - center) * step / 60 * (value - average)
                      for index, value in enumerate(values)) / denominator
    return [round(value, 6) for value in (
        values[-1], average, math.sqrt(variance), min(values), max(values), values[-1] - values[0], slope)]


async def query_range(client, expression, start, end, step):
    try:
        response = await client.get("api/v1/query_range", params={
            "query": expression, "start": start, "end": end, "step": step, "timeout": "5s"})
    except httpx.RequestError as exc:
        raise PrometheusUnavailable("Connexion Prometheus indisponible.") from exc
    if response.status_code >= 500:
        raise PrometheusUnavailable("Erreur serveur Prometheus.")
    if response.status_code != 200 or len(response.content) > MAX_JSON_BYTES:
        raise PrometheusInvalidResponse("Reponse historique Prometheus invalide.")
    try:
        payload = strict_json(response.content)
        if payload["status"] != "success" or payload["data"]["resultType"] != "matrix":
            raise ValueError("Resultat historique invalide.")
        results = payload["data"]["result"]
        if not isinstance(results, list) or len(results) > 1:
            raise ValueError("Series historiques ambigues.")
        if not results:
            return {}
        samples = results[0]["values"]
        if not isinstance(samples, list) or len(samples) > (end - start) // step + 1:
            raise ValueError("Trop d'echantillons.")
        values, previous = {}, start - step
        for sample in samples:
            if not isinstance(sample, list) or len(sample) != 2 or not isinstance(sample[1], str):
                raise ValueError("Echantillon invalide.")
            timestamp = finite_number(sample[0])
            if timestamp != int(timestamp) or not start <= timestamp <= end or (timestamp - start) % step or timestamp <= previous:
                raise ValueError("Grille historique invalide.")
            previous = timestamp
            value = float(sample[1])
            values[int(timestamp)] = value if math.isfinite(value) else None
        return values
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise PrometheusInvalidResponse("Historique Prometheus inexploitable.") from exc


def feature_vector(rows, step, lookback):
    """Reproduire les 21 variables de prepare_features sur une fenetre passee."""
    points = lookback // step + 1
    if len(rows) < points:
        raise ValueError("Fenetre de variables incomplete.")
    window = rows[-points:]
    values = []
    for metric in METRICS:
        samples = [finite_number(row[metric]) for row in window]
        if any(not 0 <= value <= 100 for value in samples):
            raise ValueError("Ressource hors de l'intervalle [0,100].")
        values.extend(window_statistics(samples, step))
    return values


@dataclass
class ResourceHistory:
    start: int
    end: int
    rows: list
    collection: object
    invalid_points: list
    reason: str | None


async def current_collection(prometheus, now):
    try:
        up, age = await asyncio.wait_for(asyncio.gather(
            prometheus.query('up{job="linux"}', now.timestamp()),
            prometheus.query('time() - timestamp(up{job="linux"})', now.timestamp())), timeout=6)
    except asyncio.TimeoutError as exc:
        raise PrometheusUnavailable("Delai global Prometheus depasse.") from exc
    return prometheus.source_status(up, age)


async def read_resource_history(prometheus, now, *, step, lookback, context):
    end = int(now.timestamp()) // step * step
    start = end - lookback - context
    queries = {metric: METRIC_QUERIES[metric] for metric in METRICS}
    queries["linux_up"] = 'up{job="linux"}'
    queries["linux_age"] = 'time() - timestamp(up{job="linux"})'
    requests = [query_range(prometheus.client, expression, start, end, step)
                for expression in queries.values()]
    requests += [prometheus.query(queries["linux_up"], now.timestamp()),
                 prometheus.query(queries["linux_age"], now.timestamp())]
    try:
        results = await asyncio.wait_for(asyncio.gather(*requests, return_exceptions=True), timeout=6)
    except asyncio.TimeoutError as exc:
        raise PrometheusUnavailable("Delai global Prometheus depasse.") from exc
    for result in results:
        if isinstance(result, Exception):
            raise result
    series = dict(zip(queries, results[:5]))
    status = prometheus.source_status(results[5], results[6])
    if status.status != "up":
        return ResourceHistory(start, end, [], status, [], "linux_collection_unavailable")
    rows, invalid = [], []
    for timestamp in range(start, end + 1, step):
        history_status = prometheus.source_status(series["linux_up"].get(timestamp), series["linux_age"].get(timestamp))
        row = {metric: series[metric].get(timestamp) for metric in METRICS}
        if history_status.status != "up" or any(value is None for value in row.values()):
            invalid.append({"evaluated_at": utc_iso(timestamp), "reason": "incomplete_or_unhealthy_collection"})
            continue
        if any(not 0 <= value <= 100 for value in row.values()):
            raise PrometheusInvalidResponse("Ressource hors de l'intervalle [0,100].")
        # Meme arrondi que l'export utilise pour l'entrainement.
        rows.append({metric: round(value, 3) for metric, value in row.items()})
    reason = "insufficient_contiguous_history" if invalid else None
    return ResourceHistory(start, end, rows if not invalid else [], status, invalid, reason)
