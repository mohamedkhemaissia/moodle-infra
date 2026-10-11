"""Lire le socket Docker local et publier un fichier atomique pour Node Exporter."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import fcntl
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import tempfile
import time
from urllib.parse import urlencode

LABEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}")
MAX_TARGETS = 24
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def targets_from_config(document):
    targets = document.get("targets") if isinstance(document, dict) else None
    if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_TARGETS:
        raise ValueError("Liste de services invalide.")
    result = []
    for item in targets:
        if (not isinstance(item, dict) or set(item) != {"project", "service"}
                or any(not isinstance(item[key], str) or not LABEL.fullmatch(item[key])
                       for key in ("project", "service"))):
            raise ValueError("Identite Compose invalide.")
        key = (item["project"], item["service"])
        if key in result:
            raise ValueError("Service configure deux fois.")
        result.append(key)
    return result


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class DockerClient:
    def __init__(self, path="/var/run/docker.sock", timeout=3):
        if not Path(path).is_absolute():
            raise ValueError("Socket local absolu requis.")
        self.path, self.timeout = str(path), timeout

    def get(self, path):
        # Seules des lectures HTTP locales sont exposees par ce client.
        if not re.fullmatch(r"/version|/v1\.[0-9]+/containers/(json\?.*|[0-9a-f]{64}/stats\?stream=false)", path):
            raise ValueError("Lecture Docker non autorisee.")
        connection = UnixConnection(self.path, self.timeout)
        try:
            connection.request("GET", path, headers={"Accept": "application/json"})
            response = connection.getresponse()
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if response.status != 200 or len(payload) > MAX_RESPONSE_BYTES:
                raise ValueError("Lecture Docker indisponible.")
            return json.loads(payload)
        finally:
            connection.close()

    def inventory(self):
        version = self.get("/version")
        api = version.get("ApiVersion") if isinstance(version, dict) else None
        if not isinstance(api, str) or not re.fullmatch(r"1\.[0-9]+", api):
            raise ValueError("Version d'API Docker invalide.")
        self.api = api
        query = urlencode({"all": "true", "filters": json.dumps({"label": ["com.docker.compose.project"]})})
        result = self.get("/v" + api + "/containers/json?" + query)
        if not isinstance(result, list) or len(result) > 1000:
            raise ValueError("Inventaire Docker invalide.")
        return result

    def stats(self, identity):
        return self.get("/v" + self.api + "/containers/" + identity + "/stats?stream=false")


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Mesure Docker invalide.")
    return value


def stats_reading(document, identity, now):
    if not isinstance(document, dict) or document.get("id") != identity:
        raise ValueError("Identite des statistiques incompatible.")
    # Docker utilise RFC3339Nano ; normaliser pour le Python 3.10 d'Ubuntu 22.04.
    raw = document["read"]
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.([0-9]{1,9}))?(Z|[+-]\d{2}:\d{2})", raw)
    if match is None:
        raise ValueError("Date des statistiques invalide.")
    fraction = (match[2] or "0")[:6].ljust(6, "0")
    stamp = datetime.fromisoformat(match[1] + "." + fraction + match[3].replace("Z", "+00:00"))
    if stamp.tzinfo is None or not -5 <= now - stamp.timestamp() <= 90:
        raise ValueError("Statistiques Docker anciennes ou incoherentes.")
    memory = document.get("memory_stats", {})
    usage, limit = number(memory["usage"]), number(memory["limit"])
    cache_values = memory.get("stats", {})
    cache = number(cache_values.get("total_inactive_file", cache_values.get("inactive_file", 0)))
    usage -= cache if cache < usage else 0
    result = {"stats_timestamp_seconds": stamp.timestamp(), "memory_working_set_bytes": usage}
    if limit > 0:
        result["memory_limit_bytes"] = limit
    current, previous = document.get("cpu_stats", {}), document.get("precpu_stats", {})
    try:
        cpu_delta = number(current["cpu_usage"]["total_usage"]) - number(previous["cpu_usage"]["total_usage"])
        system_delta = number(current["system_cpu_usage"]) - number(previous["system_cpu_usage"])
        cores = number(current.get("online_cpus", len(current["cpu_usage"].get("percpu_usage", []))))
        if cpu_delta >= 0 and system_delta > 0 and cores > 0:
            result["cpu_usage_percent"] = cpu_delta / system_delta * cores * 100
    except KeyError:
        pass  # Un premier echantillon ne prouve pas une consommation CPU nulle.
    pids = document.get("pids_stats", {}).get("current")
    if pids is not None:
        result["pids"] = number(pids)
    return result


def sample(name, value, key=None):
    # Le job Linux possede deja le label service=moodle-host. Utiliser un nom
    # distinct pour conserver l'identite Compose apres le scrape Prometheus.
    labels = "" if key is None else '{project="' + key[0] + '",compose_service="' + key[1] + '"}'
    return "moodle_container_" + name + labels + " " + format(value, ".12g") + "\n"


def collect(targets, client, clock=time.time):
    started = clock()
    try:
        inventory = client.inventory()
        selected = {key: [] for key in targets}
        for container in inventory:
            if not isinstance(container, dict) or not isinstance(container.get("Labels"), dict):
                raise ValueError("Inventaire Docker invalide.")
            labels = container["Labels"]
            key = (labels.get("com.docker.compose.project"), labels.get("com.docker.compose.service"))
            if key not in selected or labels.get("com.docker.compose.oneoff", "false").lower() == "true":
                continue
            if not re.fullmatch(r"[0-9a-f]{64}", container.get("Id", "")) or container.get("State") not in (
                    "created", "running", "paused", "restarting", "removing", "exited", "dead"):
                raise ValueError("Etat du conteneur invalide.")
            selected[key].append(container)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException):
        return (sample("collection_success", 0) + sample("attempt_timestamp_seconds", clock()),
                {"status": "unavailable", "reason": "docker_inventory_unavailable"})
    lines = [sample("collection_success", 1), sample("inventory_timestamp_seconds", clock()),
             sample("attempt_timestamp_seconds", clock())]
    running = {}
    for key, instances in selected.items():
        lines.extend([sample("instances", len(instances), key), sample("present", bool(instances), key)])
        if len(instances) != 1:
            continue
        container = instances[0]
        lines.append(sample("running", container["State"] == "running", key))
        lines.append(sample("paused", container["State"] == "paused", key))
        status = container.get("Status", "")
        if isinstance(status, str):
            health = 1 if "(healthy)" in status else 0 if "(unhealthy)" in status else 2 if "(health: starting)" in status else None
            if health is not None:
                lines.append(sample("health", health, key))
        if container["State"] == "running":
            running[key] = container["Id"]

    def read(item):
        key, identity = item
        try:
            values = stats_reading(client.stats(identity), identity, clock())
            return key, values
        except (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException):
            return key, None

    failed = 0
    with ThreadPoolExecutor(max_workers=4) as executor:
        for key, values in executor.map(read, running.items()):
            lines.append(sample("stats_success", values is not None, key))
            if values is None:
                failed += 1
            else:
                lines.extend(sample(name, value, key) for name, value in values.items())
    lines.append(sample("collector_duration_seconds", max(0, clock() - started)))
    return "".join(lines), {"status": "partial" if failed else "available",
                            "targets": len(targets), "stats_collected": len(running) - failed,
                            "stats_failed": failed}


def atomic_write(path, content):
    path = Path(path)
    if not path.is_absolute() or path.suffix != ".prom" or not path.parent.is_dir() or path.is_symlink():
        raise ValueError("Destination textfile invalide.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".containers-",
                                         dir=path.parent, delete=False) as handle:
            temporary = handle.name
            os.fchmod(handle.fileno(), 0o644)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--socket", default="/var/run/docker.sock")
    parser.add_argument("--validate-config", action="store_true")
    args = parser.parse_args()
    try:
        targets = targets_from_config(json.loads(args.config.read_text(encoding="utf-8")))
        if args.validate_config:
            print(json.dumps({"status": "validated", "targets": len(targets)}))
            return 0
        if args.output is None:
            raise ValueError("Destination manquante.")
        with open(args.output.parent.parent / "collector.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            content, report = collect(targets, DockerClient(args.socket))
            atomic_write(args.output, content)
        print(json.dumps(report))
        return 1 if report["status"] == "unavailable" else 0
    except (OSError, ValueError):
        parser.exit(1, "Collecte des conteneurs arretee ; consulter le service systemd.\n")


if __name__ == "__main__":
    raise SystemExit(main())
