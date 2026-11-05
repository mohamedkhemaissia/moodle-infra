"""Sondes bornees en lecture seule ; aucune commande fournie par une requete."""
import argparse
import json
import math
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from pilotage.worker_auth import worker_headers

from .policy import Blocked, validate
from .store import strict_json

CONTAINERS = ("moodle-monitoring-prometheus-1", "moodle-monitoring-node-exporter-1",
              "moodle-monitoring-blackbox-1", "moodle-pilote-apache-1",
              "moodle-pilote-php-1", "moodle-pilote-postgres-1")


class ProbeError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        return None


def local_url(value):
    p = urlsplit(value)
    if p.scheme != "http" or p.hostname != "127.0.0.1" or p.username or p.password or \
            not p.port or p.path not in ("", "/") or p.query or p.fragment:
        raise ValueError("Seules les API HTTP locales configurees sont autorisees.")
    return value.rstrip("/")


def load_config(path):
    config = strict_json(Path(path).read_text())
    if not isinstance(config, dict) or set(config) != {"api_url", "prometheus_url", "data_dir"}:
        raise ValueError("Configuration de diagnostic invalide.")
    for key in ("api_url", "prometheus_url"):
        config[key] = local_url(config[key])
    p = Path(config["data_dir"])
    if not p.is_absolute() or p == Path("/") or p.is_symlink():
        raise ValueError("Repertoire du journal invalide.")
    return config


def get_json(url, *, timeout=5):
    try:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(Request(url, headers=worker_headers(url)), timeout=timeout) as response:
            body = response.read(262145)
            if response.status != 200:
                raise ProbeError("http_status_" + str(response.status))
            result = strict_json(body.decode("utf-8"))
            if not isinstance(result, dict):
                raise ProbeError("json_object_required")
            return result
    except HTTPError as exc:
        raise ProbeError("http_status_" + str(exc.code)) from exc
    except (OSError, ValueError, URLError, UnicodeError, RecursionError) as exc:
        raise ProbeError("network_or_invalid_response") from exc


def numeric(value):
    if isinstance(value, bool):
        raise ValueError("Valeur numerique invalide.")
    result = float(value)
    return result if math.isfinite(result) else None


def history(config, end):
    queries = {
        "cpu": '100 * (1 - avg(rate(node_cpu_seconds_total{job="linux",mode="idle"}[2m])))',
        "memory": '100 * (1 - node_memory_MemAvailable_bytes{job="linux"} / node_memory_MemTotal_bytes{job="linux"})',
        "disk": (
            '100 * (max(node_filesystem_avail_bytes{job="linux",mountpoint="/",fstype!~"tmpfs|overlay"})'
            ' / max(node_filesystem_size_bytes{job="linux",mountpoint="/",fstype!~"tmpfs|overlay"})'
            ' or max(node_filesystem_avail_bytes{job="linux",mountpoint="/workspaces",fstype!~"tmpfs|overlay"})'
            ' / max(node_filesystem_size_bytes{job="linux",mountpoint="/workspaces",fstype!~"tmpfs|overlay"}))'
        ),
        "up": 'up{job="linux"}', "age": 'time() - timestamp(up{job="linux"})',
    }
    series = {}
    for key, query in queries.items():
        document = get_json(config["prometheus_url"] + "/api/v1/query_range?" + urlencode({
            "query": query, "start": end - 420, "end": end, "step": 30}))
        if document.get("status") != "success" or document.get("data", {}).get("resultType") != "matrix":
            raise ProbeError("invalid_prometheus_history")
        rows = document["data"]["result"]
        if not isinstance(rows, list) or len(rows) > 1:
            raise ProbeError("ambiguous_prometheus_series")
        values = rows[0]["values"] if rows else []
        if len(values) > 15:
            raise ProbeError("unexpected_history_size")
        series[key] = {}
        for stamp, value in values:
            stamp = numeric(stamp)
            if stamp is None or int(stamp) != stamp or not end - 420 <= stamp <= end or (end - stamp) % 30:
                raise ProbeError("invalid_history_timestamp")
            if int(stamp) in series[key]:
                raise ProbeError("duplicate_history_timestamp")
            series[key][int(stamp)] = numeric(value)
    invalid = []
    for stamp in range(end - 420, end + 1, 30):
        point = {key: values.get(stamp) for key, values in series.items()}
        if any(point[key] is None for key in queries) or point["up"] != 1 or \
                not 0 <= point["age"] <= 60 or any(not 0 <= point[key] <= 100 for key in ("cpu", "memory", "disk")):
            invalid.append({"timestamp": stamp, **point})
    return {"status": "warning" if invalid else "ok", "required_history_seconds": 420,
            "start": end - 420, "end": end, "step_seconds": 30,
            "grid_points": 15, "invalid_points": invalid, "valid_points": 15 - len(invalid),
            "interpretation": "Un point absent, obsolete ou collecte en echec interrompt l'historique utilisable."}


def targets(config):
    data = get_json(config["prometheus_url"] + "/api/v1/targets")
    if data.get("status") != "success":
        raise ProbeError("invalid_prometheus_targets")
    rows = data["data"]["activeTargets"]
    if not isinstance(rows, list) or len(rows) > 50:
        raise ProbeError("invalid_prometheus_targets")
    output = []
    for row in rows:
        job = row["labels"].get("job")
        if job in ("prometheus", "linux", "blackbox_exporter", "moodle_https"):
            # Ne pas recopier scrapeUrl, labels libres, ni lastError (peuvent contenir des donnees non prevues).
            output.append({"job": job, "health": row["health"], "last_scrape": row["lastScrape"],
                           "duration_seconds": numeric(row["lastScrapeDuration"]),
                           "error_present": bool(row.get("lastError"))})
    healthy = len(output) == 4 and {r["job"] for r in output} == {"prometheus", "linux", "blackbox_exporter", "moodle_https"} and all(r["health"] == "up" for r in output)
    return {"status": "ok" if healthy else "warning", "targets": output}


def services():
    results = []
    for name in CONTAINERS:
        try:
            process = subprocess.run(["/usr/bin/docker", "inspect", "--format",
                                      '{{json .State}}', name], capture_output=True, text=True,
                                     timeout=5, check=False, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
            if process.returncode != 0:
                results.append({"container": name, "status": "unavailable", "reason": "docker_inspect_failed"})
                continue
            state = strict_json(process.stdout)
            results.append({"container": name, "status": "ok" if state.get("Running") is True else "warning",
                            "state": {key: state.get(key) for key in ("Status", "Running", "Restarting", "OOMKilled", "StartedAt")}})
        except (OSError, ValueError, subprocess.TimeoutExpired):
            results.append({"container": name, "status": "unavailable", "reason": "docker_unavailable_or_timeout"})
    return {"status": "ok" if all(r["status"] == "ok" for r in results) else "warning", "services": results}


def observe(function):
    try:
        return function()
    except (ProbeError, KeyError, TypeError, ValueError, OverflowError) as exc:
        # Les erreurs libres d'un daemon ou d'un serveur ne sont jamais copiees dans l'audit public.
        return {"status": "unavailable", "reason": "probe_failed", "error_type": type(exc).__name__}


def collect(config, incident_id, expected):
    # Relecture au debut effectif du playbook, apres l'attente / le demarrage de Runner.
    document = get_json(config["api_url"] + "/api/v1/decisions/" + str(incident_id))
    decision = validate(document, incident_id, time.time(), expected=expected)
    checks = {}
    profile = decision["profile"]
    if profile in ("collection", "resources", "moodle", "php_pool"):
        checks["prometheus_targets"] = observe(lambda: targets(config))
        checks["linux_history"] = observe(lambda: history(config, int(time.time()) // 30 * 30))
        checks["service_states"] = services()
    if profile in ("resources", "moodle", "php_pool"):
        checks["current_metrics"] = observe(lambda: get_json(config["api_url"] + "/api/v1/metrics/summary"))
    if profile == "php_pool":
        checks["application_metrics"] = observe(lambda: get_json(config["api_url"] + "/api/v1/metrics/applications"))
    if profile == "model":
        checks["anomaly_model"] = observe(lambda: get_json(config["api_url"] + "/api/v1/anomalies/model"))
    return {"status": "completed", "mode": "read_only", "decision": decision,
            "checks": checks, "collected_at": time.time(),
            "interpretation": "Verifications techniques ; ne prouvent pas une cause de panne et ne corrigent aucun service."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--incident-id", type=int, required=True)
    parser.add_argument("--expected", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = collect(load_config(args.config), args.incident_id, strict_json(args.expected.read_text()))
    except (Blocked, ProbeError, OSError, ValueError) as exc:
        result = {"status": "blocked", "mode": "read_only", "reason": "revalidation_failed", "error_type": type(exc).__name__}
    print(json.dumps(result, allow_nan=False, ensure_ascii=False))


if __name__ == "__main__":
    main()
