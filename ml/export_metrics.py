"""Exporter des evaluations historiques Prometheus, avec leur qualite de collecte.

Execution depuis la racine du depot :
    python3 -m ml.export_metrics --hours 6 --step-seconds 30

Bibliotheque standard uniquement ; Python 3.10 ou plus recent.
Les cellules CSV vides sont des valeurs absentes, jamais des zeros inventes.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener


VERSION = "0.1.0"
SCHEMA_VERSION = 1
MAX_POINTS = 11_000
JOBS = ("linux", "moodle_https")
METRIC_QUERIES = {
    "cpu_usage_percent": (
        '100 * (1 - avg(rate(node_cpu_seconds_total{job="linux",mode="idle"}[2m])))'
    ),
    "memory_usage_percent": (
        '100 * (1 - node_memory_MemAvailable_bytes{job="linux"}'
        ' / node_memory_MemTotal_bytes{job="linux"})'
    ),
    "disk_available_percent": (
        '100 * node_filesystem_avail_bytes{job="linux",mountpoint="/workspaces",fstype!~"tmpfs|overlay"}'
        ' / node_filesystem_size_bytes{job="linux",mountpoint="/workspaces",fstype!~"tmpfs|overlay"}'
    ),
    "moodle_available": 'probe_success{job="moodle_https"}',
    "moodle_probe_duration_ms": 'probe_duration_seconds{job="moodle_https"} * 1000',
    "moodle_http_status_code": 'probe_http_status_code{job="moodle_https"}',
}
RESOURCE_METRICS = tuple(name for name in METRIC_QUERIES if not name.startswith("moodle_"))
QUERIES = dict(METRIC_QUERIES)
for _job in JOBS:
    QUERIES[f"{_job}_up"] = f'up{{job="{_job}"}}'
    QUERIES[f"{_job}_age"] = f'time() - timestamp(up{{job="{_job}"}})'
CSV_FIELDS = (
    "evaluated_at",
    *METRIC_QUERIES,
    "linux_collection_status",
    "linux_scrape_age_seconds",
    "moodle_https_collection_status",
    "moodle_https_scrape_age_seconds",
    "resource_metrics_complete",
    "all_metrics_complete",
)


class ExportError(Exception):
    """L'export ne peut pas etre realise de facon fiable."""


@dataclass(frozen=True)
class Window:
    start: int
    end: int
    step: int

    @property
    def count(self) -> int:
        return (self.end - self.start) // self.step + 1


@dataclass
class Series:
    values: dict[int, float | None]
    labels: dict[str, str]
    non_finite_samples: int = 0
    infos: tuple[str, ...] = ()


def utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if parsed.tzinfo is None:
            raise ValueError("fuseau absent")
        timestamp = parsed.timestamp()
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("horodatage invalide")
        return timestamp
    except (ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError(
            "Utiliser un horodatage ISO 8601 avec Z ou un decalage horaire."
        ) from exc


def make_window(
    *, hours: float | None = None, start: float | None = None,
    end: float | None = None, step: int = 30, now: float | None = None,
) -> Window:
    current = time.time() if now is None else now
    if not isinstance(step, int) or isinstance(step, bool) or step < 15:
        raise ExportError("Le pas doit etre un entier d'au moins 15 secondes.")
    requested_end = current if end is None else end
    if not math.isfinite(requested_end) or requested_end < 0 or requested_end > current + 1:
        raise ExportError("La fin de la periode doit etre valide et ne pas etre dans le futur.")
    aligned_end = math.floor(requested_end / step) * step
    if start is not None:
        if hours is not None:
            raise ExportError("Choisir --hours ou --start, pas les deux.")
        if not math.isfinite(start) or start < 0:
            raise ExportError("Debut de periode invalide.")
        requested_start = start
    else:
        duration = 6.0 if hours is None else hours
        if not math.isfinite(duration) or duration <= 0:
            raise ExportError("La duree doit etre positive et finie.")
        requested_start = aligned_end - duration * 3600
    if not math.isfinite(requested_start):
        raise ExportError("La duree demandee est trop grande.")
    aligned_start = math.ceil(requested_start / step) * step
    if aligned_start < 0 or aligned_start > aligned_end:
        raise ExportError("La periode ne contient aucun instant d'evaluation valide.")
    window = Window(aligned_start, aligned_end, step)
    if window.count > MAX_POINTS:
        raise ExportError("Periode trop longue : augmenter le pas ou reduire la duree.")
    return window


def validate_prometheus_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise ExportError("L'adresse de Prometheus est invalide.") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ExportError("L'adresse de Prometheus doit etre une URL HTTP ou HTTPS.")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ExportError("L'URL Prometheus doit etre sans identifiants, parametres ni fragment.")
    return value.rstrip("/")


def parse_matrix(payload: Any, window: Window) -> Series:
    try:
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise ExportError("La requete historique Prometheus a echoue.")
        if payload.get("warnings"):
            raise ExportError("Prometheus signale un avertissement ; l'export est interrompu.")
        infos = payload.get("infos", [])
        if not isinstance(infos, list) or not all(isinstance(info, str) for info in infos):
            raise ExportError("Annotations Prometheus invalides.")
        data = payload["data"]
        if data["resultType"] != "matrix" or not isinstance(data["result"], list):
            raise ExportError("Un resultat historique de type matrix est attendu.")
        results = data["result"]
        if len(results) > 1:
            raise ExportError("Plusieurs series trouvees : l'export concerne un seul pilote.")
        if not results:
            return Series({}, {}, infos=tuple(infos))
        labels = results[0]["metric"]
        samples = results[0]["values"]
        if not isinstance(labels, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
        ) or not isinstance(samples, list):
            raise ExportError("Serie Prometheus invalide.")
        points = {}
        non_finite = 0
        previous_index = -1
        for sample in samples:
            if not isinstance(sample, list) or len(sample) != 2 or not isinstance(sample[1], str):
                raise ExportError("Echantillon Prometheus invalide.")
            timestamp = sample[0]
            if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
                raise ExportError("Horodatage d'echantillon invalide.")
            position = (timestamp - window.start) / window.step
            index = round(position)
            if abs(position - index) > 1e-6 or not 0 <= index < window.count:
                raise ExportError("Echantillon hors de la grille temporelle demandee.")
            if index <= previous_index:
                raise ExportError("Horodatages dupliques ou non ordonnes.")
            previous_index = index
            value = float(sample[1])
            if not math.isfinite(value):
                value = None
                non_finite += 1
            points[index] = value
        return Series(points, labels, non_finite, tuple(infos))
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ExportError("Reponse historique Prometheus invalide.") from exc


class PrometheusReader:
    def __init__(self, url: str, timeout: float = 25):
        self.url = validate_prometheus_url(url)
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}))

    def query(self, expression: str, window: Window) -> Series:
        parameters = urlencode({
            "query": expression, "start": window.start, "end": window.end,
            "step": window.step, "timeout": "20s",
        })
        request = Request(
            self.url + "/api/v1/query_range?" + parameters,
            headers={"Accept": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except HTTPError as exc:
            raise ExportError(f"Prometheus renvoie HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ExportError("Prometheus est inaccessible ou le delai de lecture est depasse.") from exc
        except (ValueError, UnicodeError) as exc:
            raise ExportError("Prometheus ne renvoie pas un document JSON valide.") from exc
        return parse_matrix(payload, window)


def collection_status(up: float | None, age: float | None, max_age: float) -> str:
    if up not in (0.0, 1.0) or age is None or not math.isfinite(age) or age < 0:
        return "missing"
    if age > max_age:
        return "stale"
    return "up" if up == 1 else "down"


def metric_value(name: str, value: float | None) -> float | int | None:
    if value is None or not math.isfinite(value) or value < 0:
        return None
    if name.endswith("_percent") and value > 100:
        return None
    if name == "moodle_available":
        return int(value) if value in (0.0, 1.0) else None
    if name == "moodle_http_status_code":
        return int(value) if float(value).is_integer() and (value == 0 or 100 <= value <= 599) else None
    return round(value, 3)


def build_rows(series: dict[str, Series], window: Window, max_age: float = 60) -> list[dict[str, Any]]:
    if not math.isfinite(max_age) or max_age <= 0:
        raise ExportError("Le delai de fraicheur doit etre positif et fini.")
    if set(series) != set(QUERIES):
        raise ExportError("Toutes les requetes de mesures et de qualite sont necessaires.")
    rows = []
    for index in range(window.count):
        row = {"evaluated_at": utc_iso(window.start + index * window.step)}
        statuses = {}
        for job in JOBS:
            up = series[f"{job}_up"].values.get(index)
            age = series[f"{job}_age"].values.get(index)
            status = collection_status(up, age, max_age)
            statuses[job] = status
            row[f"{job}_collection_status"] = status
            row[f"{job}_scrape_age_seconds"] = (
                round(age, 3) if age is not None and math.isfinite(age) and age >= 0 else None
            )
        for name in METRIC_QUERIES:
            job = "moodle_https" if name.startswith("moodle_") else "linux"
            raw_value = series[name].values.get(index)
            row[name] = metric_value(name, raw_value) if statuses[job] == "up" else None
        row["resource_metrics_complete"] = int(all(row[name] is not None for name in RESOURCE_METRICS))
        row["all_metrics_complete"] = int(all(row[name] is not None for name in METRIC_QUERIES))
        rows.append(row)
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "grid_rows": len(rows),
        "observed_rows": sum(any(row[name] is not None for name in METRIC_QUERIES) for row in rows),
        "resource_complete_rows": sum(row["resource_metrics_complete"] for row in rows),
        "all_metrics_complete_rows": sum(row["all_metrics_complete"] for row in rows),
        "failed_probe_rows": sum(row["moodle_available"] == 0 for row in rows),
        "unknown_probe_rows": sum(row["moodle_available"] is None for row in rows),
        "metric_coverage": {
            name: sum(row[name] is not None for row in rows) for name in METRIC_QUERIES
        },
        "collection_status_counts": {
            job: {status: sum(row[f"{job}_collection_status"] == status for row in rows)
                  for status in ("up", "down", "stale", "missing")}
            for job in JOBS
        },
    }


def export_configuration(prometheus_url: str, max_age: float) -> tuple[dict[str, Any], str]:
    if not math.isfinite(max_age) or max_age <= 0:
        raise ExportError("Le delai de fraicheur doit etre positif et fini.")
    configuration = {
        "schema_version": SCHEMA_VERSION,
        "prometheus_url": validate_prometheus_url(prometheus_url),
        "max_scrape_age_seconds": float(max_age),
        "queries": QUERIES,
    }
    encoded = json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return configuration, hashlib.sha256(encoded).hexdigest()


def export_directory(output_dir: Path, window: Window, fingerprint: str) -> Path:
    compact = lambda value: datetime.fromtimestamp(value, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return output_dir / (
        f"moodle_metrics_{compact(window.start)}_{compact(window.end)}"
        f"_step{window.step}s_{fingerprint[:12]}"
    )


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def existing_export(directory: Path, window: Window, fingerprint: str) -> dict[str, Any] | None:
    if not directory.exists():
        return None
    try:
        metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        expected_range = {"start": utc_iso(window.start), "end": utc_iso(window.end), "step_seconds": window.step}
        if metadata["schema_version"] != SCHEMA_VERSION or metadata["configuration_sha256"] != fingerprint:
            raise ExportError("La configuration de l'export existant est incoherente.")
        if metadata["range"] != expected_range or metadata["csv_sha256"] != file_sha256(directory / "metrics.csv"):
            raise ExportError("Le contenu de l'export existant est incoherent.")
        summary = metadata["summary"]
        if not isinstance(summary, dict) or summary.get("grid_rows") != window.count:
            raise ExportError("Le bilan de l'export existant est invalide.")
        resource_count = summary.get("resource_complete_rows")
        if type(resource_count) is not int or not 0 <= resource_count <= window.count:
            raise ExportError("Le bilan de l'export existant est invalide.")
        return metadata
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ExportError("L'export existant est incomplet ou invalide.") from exc


def publish_export(directory: Path, rows: list[dict[str, Any]], metadata: dict[str, Any]) -> dict[str, Any]:
    directory.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".export-", dir=directory.parent))
    try:
        csv_path = temporary / "metrics.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        metadata = {**metadata, "csv_sha256": file_sha256(csv_path)}
        (temporary / "metadata.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        # Publication des deux fichiers ensemble, dans le meme systeme de fichiers.
        os.rename(temporary, directory)
        return metadata
    except FileExistsError as exc:
        raise ExportError("Un autre export a deja publie cette periode.") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def perform_export(
    reader: PrometheusReader, window: Window, output_dir: Path,
    max_age: float = 60, progress=None,
) -> tuple[dict[str, Any], Path, bool]:
    configuration, fingerprint = export_configuration(reader.url, max_age)
    directory = export_directory(output_dir.resolve(), window, fingerprint)
    existing = existing_export(directory, window, fingerprint)
    if existing is not None:
        return existing, directory, True
    # Requetes successives pour limiter la charge du pilote de deux vCPU.
    series = {}
    for index, (name, expression) in enumerate(QUERIES.items(), start=1):
        if progress:
            progress(f"Lecture {index}/{len(QUERIES)} : {name}")
        series[name] = reader.query(expression, window)
    rows = build_rows(series, window, max_age)
    metadata = {
        **configuration,
        "tool_version": VERSION,
        "configuration_sha256": fingerprint,
        "created_at": utc_iso(time.time()),
        "range": {"start": utc_iso(window.start), "end": utc_iso(window.end), "step_seconds": window.step},
        "semantics": {
            "rows": "Instants d'evaluation Prometheus sur une grille reguliere, sans interpolation du script.",
            "missing_values": "Cellules CSV vides ; ne pas les remplacer automatiquement par zero.",
            "cpu_usage_percent": "Moyenne de l'utilisation CPU sur une fenetre de deux minutes.",
            "moodle_available": "1 = sonde reussie ; 0 = sonde echouee ; cellule vide = mesure inconnue.",
            "complete_flags": "Completude des mesures, sans jugement sur la sante de Moodle.",
            "freshness": "Filtrage par etat et age de collecte, distinct pour Linux et la sonde Moodle.",
        },
        "summary": summarize(rows),
        "series_labels": {name: item.labels for name, item in series.items()},
        "non_finite_samples": {name: item.non_finite_samples for name, item in series.items()},
        "prometheus_infos": {name: list(item.infos) for name, item in series.items() if item.infos},
    }
    published = publish_export(directory, rows, metadata)
    return published, directory, False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    period = parser.add_mutually_exclusive_group()
    period.add_argument("--hours", type=float, help="Duree a exporter ; 6 heures par defaut.")
    period.add_argument("--start", type=parse_timestamp, help="Debut ISO 8601 avec fuseau horaire.")
    parser.add_argument("--end", type=parse_timestamp, help="Fin ISO 8601 ; heure actuelle par defaut.")
    parser.add_argument("--step-seconds", type=int, default=30, help="Pas d'evaluation ; 30 secondes par defaut.")
    parser.add_argument("--max-age-seconds", type=float, default=60, help="Age maximal de collecte ; 60 secondes par defaut.")
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:9090")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "data" / "exports")
    arguments = parser.parse_args(argv)
    try:
        window = make_window(hours=arguments.hours, start=arguments.start, end=arguments.end, step=arguments.step_seconds)
        reader = PrometheusReader(arguments.prometheus_url)
        metadata, directory, reused = perform_export(
            reader, window, arguments.output_dir, arguments.max_age_seconds,
            progress=lambda message: print(message, file=sys.stderr, flush=True),
        )
        summary = {
            "status": "already_exported" if reused else "exported",
            "csv_file": str(directory / "metrics.csv"),
            "metadata_file": str(directory / "metadata.json"),
            "range": metadata["range"],
            **metadata["summary"],
        }
        if summary["resource_complete_rows"] == 0:
            summary["status"] = "no_usable_resource_data"
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0 if summary["resource_complete_rows"] > 0 else 3
    except (ExportError, OSError) as exc:
        print(f"Export interrompu : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
