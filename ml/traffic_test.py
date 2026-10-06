"""Profil progressif de GET anonymes sur la page de connexion Moodle.

Python 3.10+, bibliotheque standard. Le profil complet dure environ dix minutes.
Les phases de trafic sont des contextes d'observation, pas des etiquettes d'anomalie.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.client import HTTPConnection, HTTPException
import ipaddress
import json
import math
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from ml.export_metrics import file_sha256, parse_timestamp, utc_iso


VERSION = "0.1.0"
MAX_BODY_BYTES = 2 * 1024 * 1024
REQUEST_FIELDS = (
    "request_id", "phase", "started_at", "finished_at", "http_status",
    "success", "latency_ms", "bytes_read", "error",
)


class RunError(Exception):
    """Configuration ou observation du test invalide."""


@dataclass(frozen=True)
class Target:
    hostname: str
    port: int
    path: str
    connect_ip: str


@dataclass(frozen=True)
class Phase:
    name: str
    seconds: float
    requests_per_second: float


@dataclass
class Outcome:
    request_id: int
    phase: str
    started_at: str
    finished_at: str
    http_status: int | None
    success: int
    latency_ms: float
    bytes_read: int
    error: str | None


def target_from_url(url: str, connect_ip: str) -> Target:
    try:
        parsed = urlsplit(url)
        port = parsed.port or 443
        address = ipaddress.ip_address(connect_ip)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or
                parsed.password is not None or parsed.query or parsed.fragment or
                parsed.path != "/login/index.php" or not address.is_loopback or not 1 <= port <= 65535):
            raise ValueError("cible invalide")
        return Target(parsed.hostname, port, parsed.path, str(address))
    except ValueError as exc:
        raise RunError("Utiliser la page HTTPS /login/index.php et une IP de connexion loopback.") from exc


class LoginFields(HTMLParser):
    def __init__(self):
        super().__init__()
        self.names = set()

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            self.names.add(dict(attrs).get("name"))


class RoutedTLSConnection(HTTPConnection):
    """Connexion loopback, avec le nom Moodle pour SNI et verification TLS."""

    def __init__(self, target: Target, context: ssl.SSLContext, deadline: float):
        super().__init__(target.hostname, target.port, timeout=5)
        self.target = target
        self.context = context
        self.deadline = deadline

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("delai total depasse")
        return remaining

    def connect(self):
        raw = socket.create_connection((self.target.connect_ip, self.port), timeout=self.remaining())
        try:
            raw.settimeout(self.remaining())
            self.sock = self.context.wrap_socket(raw, server_hostname=self.host)
            self.sock.settimeout(self.remaining())
        except BaseException:
            raw.close()
            raise


class LoginClient:
    def __init__(self, target: Target, context: ssl.SSLContext, timeout: float = 5, max_body: int = MAX_BODY_BYTES):
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
            raise RunError("La verification du certificat et du nom HTTPS est obligatoire.")
        self.target = target
        self.context = context
        self.timeout = timeout
        self.max_body = max_body

    def request(self, request_id: int, phase: str) -> Outcome:
        started = time.monotonic()
        started_at = utc_iso(time.time())
        status = None
        size = 0
        error = None
        connection = RoutedTLSConnection(self.target, self.context, started + self.timeout)
        try:
            connection.request("GET", self.target.path, headers={
                "User-Agent": f"Moodle-PFE-TrafficTest/{VERSION}",
                "Accept": "text/html", "Accept-Encoding": "identity", "Connection": "close",
            })
            active_socket = connection.sock
            active_socket.settimeout(connection.remaining())
            with connection.getresponse() as response:
                status = response.status
                if status != 200:
                    error = "unexpected_http_status"
                elif "text/html" not in response.getheader("Content-Type", "").lower():
                    error = "unexpected_content_type"
                else:
                    chunks = []
                    content_length = response.getheader("Content-Length")
                    expected_size = int(content_length) if content_length is not None and content_length.isdigit() else None
                    while not response.isclosed():
                        active_socket.settimeout(connection.remaining())
                        chunk = response.read1(min(65536, self.max_body - size + 1))
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > self.max_body:
                            error = "response_too_large"
                            break
                        chunks.append(chunk)
                    if error is None and expected_size is not None and size != expected_size:
                        error = "incomplete_response"
                    if error is None:
                        parser = LoginFields()
                        parser.feed(b"".join(chunks).decode("utf-8", errors="replace"))
                        if not {"username", "password"}.issubset(parser.names):
                            error = "login_form_missing"
        except ssl.SSLCertVerificationError:
            error = "tls_verification_failed"
        except ssl.SSLError:
            error = "tls_error"
        except TimeoutError:
            error = "timeout"
        except HTTPException:
            error = "http_protocol_error"
        except OSError:
            error = "network_error"
        finally:
            connection.close()
        return Outcome(
            request_id, phase, started_at, utc_iso(time.time()), status,
            int(error is None), round((time.monotonic() - started) * 1000, 3), size, error,
        )


class MetricsReader:
    def __init__(self, url: str):
        try:
            parsed = urlsplit(url)
            if (parsed.scheme != "http" or not ipaddress.ip_address(parsed.hostname).is_loopback or
                    parsed.username is not None or parsed.password is not None or parsed.query or
                    parsed.fragment or parsed.path != "/api/v1/metrics/summary"):
                raise ValueError("URL invalide")
            parsed.port
        except (ValueError, TypeError) as exc:
            raise RunError("L'API de mesures doit etre accessible en HTTP sur une IP loopback.") from exc
        self.url = url
        self.opener = build_opener(ProxyHandler({}))

    def read(self) -> dict:
        try:
            request = Request(self.url, headers={"Accept": "application/json"})
            with self.opener.open(request, timeout=5) as response:
                content = response.read(131073)
            if len(content) > 131072:
                raise RunError("Reponse de mesures trop grande.")
            def reject_non_finite(value):
                raise RunError("Valeur non finie dans la reponse de mesures.")
            return json.loads(content, parse_constant=reject_non_finite)
        except (HTTPError, URLError, OSError, ValueError) as exc:
            raise RunError("API de mesures inaccessible ou reponse invalide.") from exc


class Guard:
    rules = {
        "max_cpu_percent": 90, "high_cpu_samples": 3,
        "max_memory_percent": 85, "min_disk_available_percent": 10,
        "max_probe_duration_ms": 2000, "slow_probe_samples": 2,
        "max_scrape_age_seconds": 60, "max_consecutive_request_failures": 3,
    }

    def __init__(self):
        self.high_cpu = 0
        self.slow_probe = 0

    def evaluate(self, snapshot: dict) -> str | None:
        try:
            if snapshot["source"] != "prometheus":
                return "metrics_source_invalid"
            if not isinstance(snapshot["evaluated_at"], str):
                return "metrics_missing_or_invalid"
            age = time.time() - parse_timestamp(snapshot["evaluated_at"])
            if not -30 <= age <= 60:
                return "metrics_snapshot_stale"
            for job in ("linux", "moodle_https"):
                source = snapshot["collection"][job]
                scrape_age = source["scrape_age_seconds"]
                if (source["status"] != "up" or isinstance(scrape_age, bool) or
                        not isinstance(scrape_age, (int, float)) or not math.isfinite(scrape_age) or
                        not 0 <= scrape_age <= 60):
                    return "metrics_collection_unavailable"
            metrics = snapshot["metrics"]
            for name in ("cpu_usage_percent", "memory_usage_percent", "disk_available_percent", "moodle_probe_duration_ms"):
                value = metrics[name]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    return "metrics_missing_or_invalid"
                if name.endswith("_percent") and value > 100:
                    return "metrics_missing_or_invalid"
            if metrics["moodle_available"] is not True or metrics["moodle_http_status_code"] != 200:
                return "moodle_probe_failed"
            if metrics["memory_usage_percent"] >= self.rules["max_memory_percent"]:
                return "memory_limit_reached"
            if metrics["disk_available_percent"] <= self.rules["min_disk_available_percent"]:
                return "disk_space_low"
            self.high_cpu = self.high_cpu + 1 if metrics["cpu_usage_percent"] >= self.rules["max_cpu_percent"] else 0
            self.slow_probe = self.slow_probe + 1 if metrics["moodle_probe_duration_ms"] >= self.rules["max_probe_duration_ms"] else 0
            if self.high_cpu >= self.rules["high_cpu_samples"]:
                return "cpu_sustained_high"
            if self.slow_probe >= self.rules["slow_probe_samples"]:
                return "moodle_probe_sustained_slow"
            return None
        except (KeyError, TypeError, ValueError, argparse.ArgumentTypeError):
            return "metrics_missing_or_invalid"


def describe_requests(outcomes: list[Outcome]) -> dict:
    latencies = sorted(outcome.latency_ms for outcome in outcomes)
    return {
        "completed_requests": len(outcomes),
        "successful_requests": sum(outcome.success for outcome in outcomes),
        "failed_requests": sum(not outcome.success for outcome in outcomes),
        "http_status_counts": dict(Counter(str(outcome.http_status) for outcome in outcomes if outcome.http_status is not None)),
        "error_counts": dict(Counter(outcome.error for outcome in outcomes if outcome.error)),
        "latency_ms": {
            "median": round((latencies[(len(latencies) - 1) // 2] + latencies[len(latencies) // 2]) / 2, 3) if latencies else None,
            "p95": latencies[math.ceil(len(latencies) * 0.95) - 1] if latencies else None,
            "max": latencies[-1] if latencies else None,
        },
    }


class TrafficRun:
    def __init__(self, client: LoginClient, reader: MetricsReader, phases: list[Phase], output_dir: Path,
                 *, workers: int = 2, guard_interval: float = 15, progress=None):
        if not 1 <= workers <= 2 or not phases or phases[-1].requests_per_second != 0:
            raise RunError("Limiter les workers a deux et terminer par une phase de recuperation sans trafic.")
        if any(not math.isfinite(p.seconds) or p.seconds <= 0 or not 0 <= p.requests_per_second <= 4 for p in phases):
            raise RunError("Duree invalide ou cadence superieure a quatre requetes par seconde.")
        if not math.isfinite(guard_interval) or guard_interval <= 0:
            raise RunError("Intervalle de surveillance invalide.")
        self.client, self.reader, self.phases = client, reader, phases
        self.output_dir, self.workers, self.guard_interval = output_dir, workers, guard_interval
        self.progress = progress or (lambda message: None)
        self.guard = Guard()
        self.pending = {}
        self.results = defaultdict(list)
        self.phase_reports = []
        self.samples = []
        self.sequence = 0
        self.failure_streak = 0
        self.next_guard = 0.0

    def observe(self, phase: str) -> str | None:
        try:
            snapshot = self.reader.read()
            reason = self.guard.evaluate(snapshot)
        except RunError:
            snapshot, reason = None, "metrics_api_unavailable"
        sample = {"observed_at": utc_iso(time.time()), "phase": phase, "stop_reason": reason, "snapshot": snapshot}
        self.samples.append(sample)
        self.metrics_stream.write(json.dumps(sample, allow_nan=False) + "\n")
        self.metrics_stream.flush()
        if reason:
            self.progress(f"Observation {phase} : {reason}")
        elif len(self.samples) % 2 == 0:
            metrics = snapshot["metrics"]
            completed = sum(len(outcomes) for outcomes in self.results.values())
            self.progress(
                f"Observation {phase} : CPU (2m) {metrics['cpu_usage_percent']:.1f}%, "
                f"RAM {metrics['memory_usage_percent']:.1f}%, {completed} requetes terminees"
            )
        self.next_guard = time.monotonic() + self.guard_interval
        return reason

    def record(self, outcome: Outcome):
        self.results[outcome.phase].append(outcome)
        self.writer.writerow(asdict(outcome))
        self.requests_stream.flush()
        self.failure_streak = 0 if outcome.success else self.failure_streak + 1

    def collect(self, *, wait_all: bool = False):
        for future in list(self.pending):
            if wait_all or future.done():
                self.record(future.result())
                del self.pending[future]

    def phase(self, phase: Phase, *, enforce_guard: bool = True) -> str | None:
        started = time.monotonic()
        record = {**asdict(phase), "started_at": utc_iso(time.time()), "offers_skipped_busy": 0}
        self.phase_reports.append(record)
        self.progress(f"Phase {phase.name} : {phase.seconds:g}s, maximum {phase.requests_per_second:g} requetes/s")
        deadline = started + phase.seconds
        next_offer = started
        reason = None
        try:
            while time.monotonic() < deadline:
                self.collect()
                now = time.monotonic()
                if phase.requests_per_second and self.failure_streak >= Guard.rules["max_consecutive_request_failures"]:
                    reason = "consecutive_request_failures"
                    break
                if now >= self.next_guard:
                    observed_reason = self.observe(phase.name)
                    if observed_reason and enforce_guard:
                        reason = observed_reason
                        break
                    now = time.monotonic()
                if phase.requests_per_second and now < deadline and now >= next_offer:
                    if len(self.pending) < self.workers:
                        self.sequence += 1
                        future = self.pool.submit(self.client.request, self.sequence, phase.name)
                        self.pending[future] = phase.name
                    else:
                        record["offers_skipped_busy"] += 1
                    # Pas de rafale de rattrapage si l'observation ou une requete prend du temps.
                    next_offer = now + 1 / phase.requests_per_second
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(0.05, remaining))
            self.collect(wait_all=True)
            if phase.requests_per_second and self.failure_streak >= Guard.rules["max_consecutive_request_failures"]:
                reason = reason or "consecutive_request_failures"
            return reason
        except KeyboardInterrupt:
            reason = "operator_interrupt"
            raise
        except Exception:
            reason = "unexpected_client_error"
            raise
        finally:
            record.update(
                ended_at=utc_iso(time.time()), elapsed_seconds=round(time.monotonic() - started, 3),
                stop_reason=reason,
                status="interrupted" if reason == "operator_interrupt" else "aborted" if reason else "completed",
            )

    def execute(self) -> tuple[dict, Path]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        directory = Path(tempfile.mkdtemp(prefix=f"moodle_traffic_{stamp}_", dir=self.output_dir.resolve()))
        status, stop_reason = "completed", None
        started_at, started = utc_iso(time.time()), time.monotonic()
        self.pool = ThreadPoolExecutor(max_workers=self.workers)
        with (directory / "requests.csv").open("w", encoding="utf-8", newline="") as requests_stream, \
                (directory / "metrics.jsonl").open("w", encoding="utf-8") as metrics_stream:
            self.requests_stream, self.metrics_stream = requests_stream, metrics_stream
            self.writer = csv.DictWriter(requests_stream, fieldnames=REQUEST_FIELDS)
            self.writer.writeheader()
            try:
                stop_reason = self.observe("preflight")
                if stop_reason:
                    status = "blocked"
                else:
                    self.sequence += 1
                    preflight = self.client.request(self.sequence, "preflight")
                    self.record(preflight)
                    if not preflight.success:
                        status, stop_reason = "blocked", preflight.error
                    else:
                        for phase in self.phases[:-1]:
                            stop_reason = self.phase(phase)
                            if stop_reason:
                                status = "aborted"
                                self.progress(f"Arret du trafic : {stop_reason}. Observation de la recuperation.")
                                break
                        self.phase(self.phases[-1], enforce_guard=False)
            except KeyboardInterrupt:
                status, stop_reason = "interrupted", "operator_interrupt"
            except Exception:
                status, stop_reason = "error", "unexpected_client_error"
                raise
            finally:
                self.pool.shutdown(wait=True, cancel_futures=True)
                for future in list(self.pending):
                    if not future.cancelled():
                        self.record(future.result())
                    del self.pending[future]
                requests_stream.flush()
                metrics_stream.flush()
                all_results = [outcome for outcomes in self.results.values() for outcome in outcomes]
                for phase in self.phase_reports:
                    outcomes = self.results[phase["name"]]
                    phase.update(describe_requests(outcomes))
                    phase["achieved_requests_per_second"] = round(len(outcomes) / phase["elapsed_seconds"], 3) if phase["elapsed_seconds"] > 0 else None
                maxima = {}
                for name in ("cpu_usage_percent", "memory_usage_percent", "moodle_probe_duration_ms"):
                    metric_sets = [sample["snapshot"].get("metrics") for sample in self.samples if isinstance(sample["snapshot"], dict)]
                    values = [metrics.get(name) for metrics in metric_sets if isinstance(metrics, dict)]
                    finite = [value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0]
                    maxima[name] = max(finite) if finite else None
                report = {
                    "tool_version": VERSION, "status": status, "stop_reason": stop_reason,
                    "started_at": started_at, "finished_at": utc_iso(time.time()),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "generator_hostname": socket.gethostname(),
                    "target": asdict(self.client.target), "metrics_url": self.reader.url,
                    "workers": self.workers, "request_timeout_seconds": self.client.timeout,
                    "guard_interval_seconds": self.guard_interval, "guard_rules": Guard.rules,
                    "requests": describe_requests(all_results), "observed_maxima": maxima,
                    "last_guard_stop_reason": self.samples[-1]["stop_reason"] if self.samples else None,
                    "phases": self.phase_reports,
                    "artifacts": {name: file_sha256(directory / name) for name in ("requests.csv", "metrics.jsonl")},
                    "semantics": {
                        "workload": "GET anonyme de la page de connexion, nouvelle connexion TLS par requete, sans cookies ni redirections.",
                        "success": "HTTP 200, HTML avec champs username/password, certificat et nom HTTPS verifies.",
                        "latency": "Temps client total TCP, TLS, lecture et validation HTML ; echecs inclus ; p95 par rang le plus proche.",
                        "load_rate": "Cadence maximale visee ; debit reel mesure, sans file de requetes non bornee ni rafale de rattrapage.",
                        "guard_rules": "Limites prudentes du pilote ; ne constituent pas des engagements de service.",
                        "phases": "Contextes de trafic, sans etiquette automatique normal/anormal.",
                        "scope": "Ce profil ne mesure pas les connexions authentifiees, les quiz ni une capacite en nombre d'apprenants.",
                        "generator": "Execution sur la machine du generateur ; si elle heberge Moodle, ses ressources incluent aussi le client de test.",
                        "cpu": "L'API CPU fournit une moyenne sur deux minutes, a prendre en compte pour les transitions entre phases.",
                    },
                }
                temporary = directory / "report.json.tmp"
                temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
                temporary.replace(directory / "report.json")
        return report, directory


def profile(name: str) -> list[Phase]:
    if name == "smoke":
        return [Phase("baseline", 15, 0), Phase("traffic_1rps", 30, 1), Phase("recovery", 15, 0)]
    return [Phase("baseline", 120, 0), Phase("traffic_2rps", 180, 2), Phase("traffic_4rps", 180, 4), Phase("recovery", 120, 0)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://moodle-pfe.test:8443/login/index.php")
    parser.add_argument("--connect-ip", default="127.0.0.1")
    parser.add_argument("--ca-file", type=Path, default=Path("/opt/moodle-monitoring/blackbox/moodle-ca.crt"))
    parser.add_argument("--metrics-url", default="http://127.0.0.1:8000/api/v1/metrics/summary")
    parser.add_argument("--profile", choices=("smoke", "full"), default="full")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "data/traffic")
    arguments = parser.parse_args(argv)
    try:
        target = target_from_url(arguments.url, arguments.connect_ip)
        context = ssl.create_default_context(cafile=str(arguments.ca_file))
        context.set_alpn_protocols(["http/1.1"])
        run = TrafficRun(
            LoginClient(target, context), MetricsReader(arguments.metrics_url), profile(arguments.profile), arguments.output_dir,
            progress=lambda message: print(message, file=sys.stderr, flush=True),
        )
        report, directory = run.execute()
        print(json.dumps({
            "status": report["status"], "stop_reason": report["stop_reason"],
            "report_file": str(directory / "report.json"),
            "elapsed_seconds": report["elapsed_seconds"],
            "requests": report["requests"], "observed_maxima": report["observed_maxima"],
            "phases": [{name: phase[name] for name in (
                "name", "completed_requests", "failed_requests", "achieved_requests_per_second", "latency_ms",
            )} for phase in report["phases"]],
        }, indent=2, ensure_ascii=False))
        return 0 if report["status"] == "completed" else 130 if report["status"] == "interrupted" else 3
    except (RunError, OSError, ValueError) as exc:
        print(f"Test interrompu ou non lance : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
