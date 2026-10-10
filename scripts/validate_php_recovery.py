"""Un scenario de validation du pilote ; jamais une boucle de production."""
import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

API = "http://127.0.0.1:8000"
CONFIG = Path("/opt/moodle-remediation/config.json")
CODE = Path("/opt/moodle-remediation/code")
WORKER = "/usr/local/sbin/moodle-remediate"
POLICY_SHA = "d70076762e038a0d7758838d0f3d706258a3b4306707b832a893f66c9faadf52"


class Refused(Exception):
    pass


def same_running(container, identifier=None):
    state = container["state"]
    return (identifier is None or container["id"] == identifier) and \
        state["Status"] == "running" and state["Running"] is True and \
        all(state.get(key) is False for key in ("Restarting", "Paused", "OOMKilled", "Dead"))


def candidate(document, previous_ids, fault_at):
    if document.get("has_more") is not False:
        raise Refused("incident_list_incomplete")
    values = [item for item in document["items"] if item["signal_key"] == "moodle_https"
              and item["status"] == "open" and item["latest_state"] == "triggered"
              and item["id"] not in previous_ids
              and datetime.fromisoformat(item["first_observed_at"].replace("Z", "+00:00")).timestamp() >= fault_at - 30]
    if len(values) > 1:
        raise Refused("ambiguous_https_incidents")
    return values[0]["id"] if values else None


class Lab:
    def __init__(self):
        sys.path.insert(0, str(CODE))
        if Path("/etc/moodle-pilotage/active").is_file():
            os.environ.setdefault("PILOTAGE_DATABASE_CONFIG", "/etc/moodle-pilotage/database.json")
        from remediation.action import config_read, operations_clear, inspect, command
        from remediation.policy import metric_gate, decision_for_php
        from remediation.probes import login_probe
        from remediation.store import RemediationStore
        from diagnostics.collector import get_json
        from diagnostics.policy import Blocked
        self.config = config_read(CONFIG)
        if self.config["api_url"] != API or self.config["hostname"] != "moodle-pfe.test" or self.config["https_port"] != 8443:
            raise Refused("unexpected_pilot_configuration")
        self.inspect = inspect
        self.command = command
        self.operations_clear = lambda: operations_clear(self.config)
        self.json = lambda path: get_json(API + path)
        self.metric_gate = metric_gate
        self.decision_for_php = decision_for_php
        self.blocked = Blocked
        self.probe = lambda: login_probe(self.config["hostname"], 8443, self.config["ca_file"])
        self.audit = RemediationStore(self.config["data_dir"])

    def claim_ready(self):
        with self.audit.connect() as connection:
            last = connection.execute("SELECT MAX(claimed_at) FROM claims").fetchone()[0]
        if last is not None and time.time() - last < 300:
            raise Refused("remediation_cooldown_or_clock_inconsistent")
        with (Path(self.config["data_dir"]) / "worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Refused("remediation_worker_already_running") from exc

    def preflight(self):
        self.operations_clear()
        self.claim_ready()
        health = self.json("/api/v1/health")
        if health != {"status": "ok", "service": "moodle-control-plane", "version": "0.7.0"}:
            raise Refused("unexpected_api_version")
        metrics = self.json("/api/v1/metrics/summary")
        self.metric_gate(metrics, time.time(), available=True)
        m = metrics["metrics"]
        if m["cpu_usage_percent"] >= 80 or m["memory_usage_percent"] >= 90 or m["disk_available_percent"] <= 10:
            raise Refused("resources_not_ready_for_test")
        decisions = self.json("/api/v1/decisions/summary")
        if decisions.get("snapshot_usable") is not True or decisions.get("policy_sha256") != POLICY_SHA:
            raise Refused("observer_or_policy_not_ready")
        incidents = self.json("/api/v1/incidents?status=open&limit=100")
        if incidents.get("has_more") is not False or any(item["signal_key"] == "moodle_https" for item in incidents["items"]):
            raise Refused("https_incident_already_open_or_list_incomplete")
        containers = {name: self.inspect(name) for name in ("php", "apache", "postgres")}
        if not all(same_running(item) for item in containers.values()) or containers["postgres"]["health"] != "healthy" or containers["php"]["offline_maintenance"]:
            raise Refused("containers_not_ready")
        if self.probe().get("success") is not True:
            raise Refused("initial_https_failed")
        return {"metrics": metrics, "containers": containers, "previous_ids": [item["id"] for item in incidents["items"]]}

    def arm(self, unit, identifier):
        try:
            result = self.command(["/usr/bin/systemd-run", "--unit=" + unit, "--on-active=5m",
                                   "--timer-property=AccuracySec=1s", "--property=Type=oneshot",
                                   "--property=RemainAfterExit=yes", "--property=TimeoutStartSec=30s",
                                   "/usr/bin/docker", "start", identifier], timeout=15)
            if result.returncode or self.command(["/usr/bin/systemctl", "is-active", unit + ".timer"]).returncode:
                raise Refused("fallback_timer_not_armed")
        except Exception:
            # Aucun arret n'a encore ete demande ; annuler un armement incomplet.
            self.command(["/usr/bin/systemctl", "stop", unit + ".timer"], timeout=10)
            raise

    def fallback_used(self, unit):
        result = self.command(["/usr/bin/systemctl", "show", unit + ".service",
                               "--property=ExecMainStartTimestampMonotonic", "--value"])
        if result.returncode or not re.fullmatch(r"\d+", result.stdout.strip()):
            raise Refused("fallback_state_unavailable")
        return int(result.stdout.strip()) != 0

    def worker(self, identifier):
        # La commande de production fait toutes ses propres revalidations.
        result = subprocess.run([WORKER, "--incident-id", str(identifier)],
                                capture_output=True, text=True, timeout=150)
        try:
            value = json.loads(result.stdout)
        except ValueError as exc:
            raise Refused("worker_output_invalid") from exc
        return {"exit_code": result.returncode, "result": value}

    def cleanup(self, unit, identifier, injected):
        if injected:
            state = self.inspect("php")
            if state["id"] != identifier:
                raise Refused("php_replaced_during_test_leave_fallback_for_original_id")
            if not same_running(state, identifier):
                if self.command(["/usr/bin/docker", "start", identifier], timeout=15).returncode:
                    raise Refused("manual_fallback_start_failed_timer_retained")
            if not same_running(self.inspect("php"), identifier):
                raise Refused("php_running_not_confirmed_timer_retained")
        # On ne supprime le secours qu'apres confirmation du PHP initial en fonctionnement.
        result = self.command(["/usr/bin/systemctl", "stop", unit + ".timer"], timeout=10)
        return {"php_running": bool(injected), "fallback_timer_stopped": result.returncode == 0}


def verify_action(detail, identifier, incident_id, fault_at):
    claim = detail.get("action_reservation") or {}
    report = detail.get("report") or {}
    return detail.get("status") == "completed" and detail.get("incident_id") == incident_id and \
        claim.get("container_id") == identifier and claim.get("incident_id") == incident_id and \
        claim.get("claimed_at", 0) >= fault_at and report.get("action_dispatched") is True and \
        report.get("recovered") is True and report.get("reason") == "https_and_new_probe_confirmed"


def scenario(lab, report, save, *, inject=False, pause=time.sleep, clock=time.monotonic):
    unit = "pfe-php-test-secours-" + uuid.uuid4().hex[:12]
    armed = injected = False
    identifier = None
    try:
        baseline = lab.preflight()
        report.update(status="ready", baseline=baseline, mode="controlled_fault_test" if inject else "read_only_preflight")
        save()
        if not inject:
            return
        identifier = baseline["containers"]["php"]["id"]
        report["fallback_unit"] = unit
        lab.arm(unit, identifier)
        armed = True
        report["fault_requested_at"] = time.time()
        save()
        # Relecture immediatement avant l'arret, apres l'armement du secours.
        latest = lab.preflight()
        if any(latest["containers"][name]["id"] != baseline["containers"][name]["id"] for name in baseline["containers"]):
            raise Refused("container_changed_before_fault")
        injected = True  # Un arret dont l'issue est inconnue impose aussi un retour de secours.
        stopped = lab.command(["/usr/bin/docker", "stop", "--time", "20", identifier], timeout=30)
        if stopped.returncode or lab.inspect("php")["state"]["Status"] != "exited":
            raise Refused("php_stop_not_confirmed")
        report["php_stopped_at"] = time.time()
        report["https_during_fault"] = lab.probe()
        report["status"] = "waiting_incident"
        save()
        deadline = clock() + 100
        incident_id = None
        for _ in range(21):
            if clock() >= deadline or lab.fallback_used(unit):
                raise Refused("incident_wait_expired_or_fallback_started")
            incident_id = candidate(lab.json("/api/v1/incidents?status=open&limit=100"),
                                    baseline["previous_ids"], report["fault_requested_at"])
            if incident_id:
                document = lab.json("/api/v1/decisions/" + str(incident_id))
                try:
                    lab.decision_for_php(document, incident_id, time.time())
                    lab.metric_gate(lab.json("/api/v1/metrics/summary"), time.time(), available=False)
                    report["decision_before_action"] = document
                    break
                except lab.blocked:
                    pass
            pause(5)
        else:
            raise Refused("no_eligible_https_incident")
        if not incident_id:
            raise Refused("no_eligible_https_incident")
        report.update(status="running_remediation", incident_id=incident_id)
        save()
        result = lab.worker(incident_id)
        report["remediation"] = result
        save()
        worker_result = result["result"]
        run_id = worker_result.get("run_id", "")
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise Refused("remediation_run_id_missing")
        detail = lab.json("/api/v1/remediations/" + run_id)
        report["audit"] = detail
        report["fallback_used"] = lab.fallback_used(unit)
        if report["fallback_used"] or result["exit_code"] != 0 or not verify_action(detail, identifier, incident_id, report["fault_requested_at"]):
            raise Refused("remediation_not_proven_or_fallback_used")
        report["https_after"] = lab.probe()
        report["metrics_after"] = lab.json("/api/v1/metrics/summary")
        lab.metric_gate(report["metrics_after"], time.time(), available=True)
        if report["https_after"].get("success") is not True or not same_running(lab.inspect("php"), identifier):
            raise Refused("final_return_not_confirmed")
        report.update(status="recovery_validated", incident_resolution="pending",
                      elapsed_to_confirmation_seconds=round(time.time() - report["fault_requested_at"], 3))
    except KeyboardInterrupt:
        report.update(status="interrupted", reason="operator_interrupt")
    except Exception as exc:
        report.update(status="inconclusive" if injected else "blocked", reason=str(exc)[:240], error_type=type(exc).__name__)
    finally:
        if armed:
            try:
                # Capturer l'usage du secours avant de l'annuler ou de rendre PHP a l'operateur.
                report["fallback_used"] = lab.fallback_used(unit)
                if report["fallback_used"] and report["status"] == "recovery_validated":
                    report.update(status="inconclusive", reason="fallback_used")
            except Exception as exc:
                report["fallback_state_error"] = str(exc)[:240]
                if report["status"] == "recovery_validated":
                    report.update(status="inconclusive", reason="fallback_state_unavailable")
            try:
                report["cleanup"] = lab.cleanup(unit, identifier, injected)
            except Exception as exc:
                report["cleanup"] = {"error": str(exc)[:240], "fallback_timer_retained": True}
                if report["status"] == "recovery_validated":
                    report.update(status="inconclusive", reason="cleanup_not_confirmed")
        report["finished_at"] = time.time()
        save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Arreter PHP puis valider sa remise en service sur le pilote.")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Lancer avec sudo sur le pilote Moodle.")
    repo = Path.cwd()
    if not (repo / ".git").exists():
        parser.error("Se placer dans ~/moodle-infra.")
    os.umask(0o027)
    directory = repo / "ml/data/validation" / ("php_recovery_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True)
    output = directory / "report.json"
    report = {"status": "starting", "started_at": time.time(), "report_file": str(output),
              "semantics": "Un test sur le pilote ; aucune mesure de disponibilite generale ou de precision ML."}
    def save():
        temporary = output.with_suffix(".json.tmp")
        with temporary.open("w") as stream:
            json.dump(report, stream, indent=2, allow_nan=False, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        temporary.replace(output)
        print("Etape :", report["status"], flush=True)
    save()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        with (Path("/var/lib/moodle-remediation") / "validation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lab = Lab()
            scenario(lab, report, save, inject=args.run)
    except Exception as exc:
        report.update(status="blocked", reason=str(exc)[:240], error_type=type(exc).__name__)
        save()
    print(json.dumps({key: report[key] for key in ("status", "report_file", "incident_id", "remediation", "elapsed_to_confirmation_seconds", "fallback_used", "cleanup", "reason") if key in report}, indent=2, ensure_ascii=False))
    return 0 if report["status"] in ("ready", "recovery_validated") else 2


if __name__ == "__main__":
    sys.exit(main())
