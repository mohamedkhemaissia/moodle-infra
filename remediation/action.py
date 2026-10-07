"""Action fixe executee par le playbook, avec preuves et verification du retour."""
import argparse
import json
import re
import subprocess
import time
from pathlib import Path

from diagnostics.collector import ProbeError, get_json, local_url
from diagnostics.policy import Blocked, timestamp
from diagnostics.store import strict_json
from .policy import decision_for_php, metric_gate
from .probes import login_probe
from .store import RemediationStore, ACTION

PROJECT = "moodle-pilote"
NAMES = {service: PROJECT + "-" + service + "-1" for service in ("php", "apache", "postgres")}


def config_read(path):
    config = strict_json(Path(path).read_text())
    if set(config) != {"api_url", "data_dir", "hostname", "https_port", "ca_file", "restoration_marker"}:
        raise ValueError("Configuration de remediation invalide.")
    config["api_url"] = local_url(config["api_url"])
    if config["hostname"] != "moodle-pfe.test" or type(config["https_port"]) is not int or not 1 <= config["https_port"] <= 65535 or \
            not Path(config["ca_file"]).is_absolute() or \
            not Path(config["restoration_marker"]).is_absolute():
        raise ValueError("Cible differente du pilote valide.")
    if not Path(config["data_dir"]).is_absolute() or config["data_dir"] in ("/", "/var/lib"):
        raise ValueError("Journal invalide.")
    return config


def command(argv, timeout=5):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False,
                          env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})


def inspect(service):
    output = command(["/usr/bin/docker", "inspect", "--format",
        '{"id":{{json .Id}},"state":{{json .State}},"labels":{{json .Config.Labels}},"mounts":{{json .Mounts}}}', NAMES[service]])
    if output.returncode:
        raise Blocked("container_inspect_failed:" + service)
    raw = strict_json(output.stdout)
    labels = raw["labels"]
    if not re.fullmatch(r"[0-9a-f]{64}", raw["id"]) or labels.get("com.docker.compose.project") != PROJECT or \
            labels.get("com.docker.compose.service") != service or labels.get("com.docker.compose.oneoff", "False").lower() != "false":
        raise Blocked("container_identity_mismatch:" + service)
    state = raw["state"]
    result = {"id": raw["id"], "service": service, "state": {key: state.get(key) for key in (
        "Status", "Running", "Restarting", "Paused", "OOMKilled", "Dead", "ExitCode", "StartedAt", "FinishedAt")},
        "health": state.get("Health", {}).get("Status")}
    if service == "php":
        # Une maintenance hors ligne Moodle peut etre identifiable dans son volume de donnees.
        result["offline_maintenance"] = any("moodledata" in mount.get("Destination", "") and
            Path(mount["Source"], "climaintenance.html").exists() for mount in raw["mounts"])
    return result


def operations_clear(config):
    if Path(config["restoration_marker"]).exists():
        raise Blocked("restoration_in_progress")
    process = command(["/usr/bin/systemctl", "list-units", "--type=service",
                       "--state=active,activating,deactivating", "--output=json", "--no-pager"])
    if process.returncode:
        raise Blocked("operations_state_unavailable")
    units = strict_json(process.stdout)
    if not isinstance(units, list):
        raise Blocked("operations_state_invalid")
    if any(re.search(r"moodle.*(backup|retention|recover|restor)", str(row.get("unit", "")), re.I) for row in units):
        raise Blocked("backup_or_recovery_service_active")


def container_gate(services):
    for name, item in services.items():
        s = item["state"]
        if any(s.get(key) is not False for key in ("Restarting", "Paused", "OOMKilled", "Dead")):
            raise Blocked("unsafe_container_state:" + name)
        if name == "php":
            if s["Status"] != "exited" or s["Running"] is not False or item.get("offline_maintenance") is not False:
                raise Blocked("php_not_stopped_or_maintenance")
        elif s["Status"] != "running" or s["Running"] is not True or item["health"] not in (None, "healthy"):
            raise Blocked("dependency_not_ready:" + name)
    if services["postgres"]["health"] != "healthy":
        raise Blocked("database_health_not_confirmed")


def current(config, incident_id, expected):
    decision = decision_for_php(get_json(config["api_url"] + "/api/v1/decisions/" + str(incident_id)),
                               incident_id, time.time(), expected=expected)
    metrics = get_json(config["api_url"] + "/api/v1/metrics/summary")
    metric_gate(metrics, time.time(), available=False)
    return decision, metrics


def apply(config, incident_id, identifier, expected, *, inspect_fn=inspect, probe_fn=None,
          operations_fn=operations_clear, start_fn=None, wait_seconds=60, pause=time.sleep):
    probe_fn = probe_fn or (lambda: login_probe(config["hostname"], config["https_port"], config["ca_file"]))
    start_fn = start_fn or (lambda container_id: command(["/usr/bin/docker", "start", container_id], timeout=15))
    report = {"status": "blocked", "mode": "manual_guarded", "action": ACTION,
              "action_dispatched": False, "recovered": False, "checks": {}}
    try:
        decision, metrics = current(config, incident_id, expected)
        report["decision"], report["checks"]["metrics_before"] = decision, metrics
        operations_fn(config)
        services = {name: inspect_fn(name) for name in NAMES}
        container_gate(services)
        report["checks"]["containers_before"] = services
        before = probe_fn()
        report["checks"]["https_before"] = before
        # Le pilote peut renvoyer HTTP 500 lorsque PHP est arrete. Ce code seul
        # ne prouve aucune cause : l'etat PHP et les dependances sont controles
        # ci-dessus, puis revalides juste avant le demarrage du conteneur identifie.
        if before["success"] is not False or before["tls_validated"] is not True or before["http_status"] not in (500,502,503,504):
            raise Blocked("php_upstream_failure_not_confirmed")
        # Revalidation apres les sondes et comparaison du conteneur juste avant l'action.
        decision, _ = current(config, incident_id, expected)
        operations_fn(config)
        latest = {name: inspect_fn(name) for name in NAMES}
        container_gate(latest)
        if any(latest[name]["id"] != services[name]["id"] for name in NAMES):
            raise Blocked("container_replaced_during_checks")
        report["decision"] = decision
        if not timestamp(decision["input_evaluated_at"]) <= time.time() < timestamp(decision["valid_until"]):
            raise Blocked("decision_expired_before_dispatch")
        reserved_at = time.time()
        reason = RemediationStore(config["data_dir"]).reserve(identifier, incident_id, reserved_at, latest["php"]["id"])
        if reason:
            raise Blocked(reason)
        report["action_reserved_at"] = reserved_at
        if not timestamp(decision["input_evaluated_at"]) <= time.time() < timestamp(decision["valid_until"]):
            raise Blocked("decision_expired_after_reservation")
        # Une reservation durable precede l'appel Docker : une issue inconnue n'est jamais rejouee automatiquement.
        report["action_dispatched"] = True
        output = start_fn(latest["php"]["id"])
        report["docker_rc"] = output.returncode
        if output.returncode != 0:
            report.update(status="failed", reason="docker_start_failed")
            return report
        deadline = time.monotonic() + wait_seconds
        consecutive = 0
        checks = []
        for _ in range(31):
            if time.monotonic() > deadline:
                break
            after = probe_fn()
            state = inspect_fn("php")
            running = state["id"] == latest["php"]["id"] and state["state"]["Running"] is True and \
                      state["state"]["Status"] == "running" and all(state["state"].get(k) is False for k in ("Restarting", "Paused", "OOMKilled", "Dead"))
            consecutive = consecutive + 1 if after.get("success") is True and after.get("tls_validated") is True and running else 0
            checks.append({"https": after, "php_running": running})
            report["checks"]["recovery_samples"] = checks
            if consecutive >= 3:
                try:
                    post = get_json(config["api_url"] + "/api/v1/metrics/summary")
                    scrape = metric_gate(post, time.time(), available=True)
                    report["checks"]["metrics_after"] = post
                    if scrape >= reserved_at:
                        report.update(status="completed", recovered=True, reason="https_and_new_probe_confirmed")
                        return report
                except (Blocked, ProbeError):
                    pass
            pause(2)
        report.update(status="failed", reason="recovery_not_confirmed_within_deadline")
    except Blocked as exc:
        report.update(status="failed" if report["action_dispatched"] else "blocked", reason=str(exc)[:240])
    except (ProbeError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        report.update(status="failed" if report["action_dispatched"] else "blocked", reason="check_or_execution_unavailable", error_type=type(exc).__name__)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--incident-id", type=int, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    result = apply(config_read(args.config), args.incident_id, args.run_id, strict_json(args.expected.read_text()))
    print(json.dumps(result, allow_nan=False, ensure_ascii=False))


if __name__ == "__main__":
    main()
