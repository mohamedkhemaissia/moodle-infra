"""Verifier la correction automatique du pilote ; controle en lecture seule par defaut."""
import argparse
import fcntl
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

_spec = importlib.util.spec_from_file_location(
    "php_validation_base", Path(__file__).with_name("validate_php_recovery.py")
)
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)
Refused = base.Refused
TIMER = "moodle-remediation-worker.timer"
SERVICE = "moodle-remediation-worker.service"
SUPERVISOR_PROGRAM = "moodle-remediation-worker"
FALLBACK_DIR = Path("/run/moodle-remediation-validation")


class ResourcePressure(Refused):
    def __init__(self, metrics):
        values = metrics["metrics"]
        limits = {"cpu_usage_percent": 80, "memory_usage_percent": 90,
                  "disk_available_percent": 10}
        blocked = [key for key, limit in limits.items()
                   if (values[key] <= limit if key == "disk_available_percent" else values[key] >= limit)]
        self.evidence = {"evaluated_at": metrics.get("evaluated_at"),
                         "measured_percent": {key: values[key] for key in limits},
                         "limits_percent": limits, "blocked_resources": blocked}
        super().__init__("resources_not_ready_for_test")


def policy_snapshot(document):
    """Valider les champs et l'empreinte observes sans modifier la politique."""
    from hashlib import sha256
    try:
        policy = document["policy"]
        fields = {k: policy[k] for k in ("revision", "mode", "suspended", "evaluation_context")}
        if document["status"] != "available" or type(fields["revision"]) is not int or fields["revision"] < 1:
            raise ValueError
        if fields["mode"] not in ("observation", "assisted", "automatic") or any(
            type(fields[k]) is not bool for k in ("suspended", "evaluation_context")
        ):
            raise ValueError
        encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if policy["sha256"] != sha256(encoded.encode()).hexdigest():
            raise ValueError
        return {**fields, "sha256": policy["sha256"]}
    except (KeyError, TypeError, ValueError):
        raise Refused("operating_policy_invalid") from None


def automatic_ready(policy):
    return policy["mode"] == "automatic" and not policy["suspended"] and not policy["evaluation_context"]


def sudo_identity():
    uid, gid = os.getenv("SUDO_UID", ""), os.getenv("SUDO_GID", "")
    if uid.isdigit() and gid.isdigit() and int(uid) > 0 and int(gid) > 0:
        return int(uid), int(gid)
    return None


def handoff_report(directory, output, identity):
    if identity is None:
        return
    os.chown(output, *identity)
    os.chown(directory, *identity)


def require_automatic(policy):
    if not automatic_ready(policy):
        raise Refused("automatic_requires_active_mode_outside_evaluation")


def same_policy(before, document):
    after = policy_snapshot(document)
    require_automatic(after)
    if after != before:
        raise Refused("operating_policy_changed_during_validation")


def verify_automatic_action(detail, php_id, incident_id, fault_at, policy):
    """Un retour HTTP seul ne prouve pas une correction automatique."""
    try:
        report = detail["report"]
        authorization = report["operating_authorization"]
        decision = detail["decision"]
        return (
            base.verify_action(detail, php_id, incident_id, fault_at)
            and detail["action_id"] == "start_php_stopped"
            and detail["action_reservation"]["run_id"] == detail["id"]
            and detail["action_reservation"]["action_id"] == "start_php_stopped"
            and detail["runner_status"] == "successful" and detail["runner_rc"] == 0
            and report["mode"] == "automatic_guarded"
            and decision["execution_mode"] == "automatic_guarded"
            and authorization["trigger_mode"] == "automatic"
            and policy_snapshot({"status": "available", "policy": authorization["policy"]}) == policy
            and policy_snapshot({"status": "available", "policy": decision["operating_policy"]}) == policy
            and authorization["approval_id"] is None
            and detail["started_at"] >= fault_at
            and detail["finished_at"] >= detail["started_at"]
            and math.isfinite(detail["finished_at"])
        )
    except (KeyError, TypeError, ValueError, Refused):
        return False


class Lab(base.Lab):
    def __init__(self):
        super().__init__()
        from diagnostics.collector import get_json
        self.json = lambda path: get_json(base.API + path, timeout=10)
        if self.config.get("policy_required") is not True:
            raise Refused("operating_policy_not_required_by_remediation")
        self.codespaces = self.config.get("operations_check_mode") == "marker_only"
        self.fallbacks = {}

    def scheduler_ready(self):
        if self.codespaces:
            process = self.command(
                ["/usr/bin/supervisorctl", "status", SUPERVISOR_PROGRAM], timeout=10
            )
            fields = process.stdout.split()
            if process.returncode or len(fields) < 2 or fields[0] != SUPERVISOR_PROGRAM or fields[1] != "RUNNING":
                raise Refused("automatic_supervisor_worker_not_active")
            return "supervisor"
        timer = self.command(["/usr/bin/systemctl", "is-active", TIMER], timeout=10)
        if timer.returncode != 0 or timer.stdout.strip() != "active":
            raise Refused("automatic_timer_not_active")
        return "systemd"

    def arm(self, unit, identifier):
        if not self.codespaces:
            return super().arm(unit, identifier)
        if not re.fullmatch(r"pfe-auto-php-secours-[0-9a-f]{12}", unit) or not re.fullmatch(
            r"[0-9a-f]{64}", identifier
        ):
            raise Refused("fallback_parameters_invalid")
        FALLBACK_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker = FALLBACK_DIR / (unit + ".json")
        log = FALLBACK_DIR / (unit + ".log")
        if marker.exists() or unit in self.fallbacks:
            raise Refused("fallback_state_already_exists")
        helper = Path(__file__).with_name("php_fallback_guard.py").resolve()
        with log.open("x", encoding="utf-8") as output:
            process = subprocess.Popen(
                [sys.executable, "-B", str(helper), "--container-id", identifier,
                 "--delay", "300", "--marker", str(marker)],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                close_fds=True, start_new_session=True,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
        os.chmod(log, 0o600)
        if process.poll() is not None:
            raise Refused("fallback_process_not_armed")
        self.fallbacks[unit] = {"process": process, "marker": marker, "log": log,
                                "container_id": identifier}

    def fallback_used(self, unit):
        if not self.codespaces:
            return super().fallback_used(unit)
        fallback = self.fallbacks.get(unit)
        if fallback is None:
            raise Refused("fallback_state_unavailable")
        if fallback["marker"].is_file():
            try:
                document = json.loads(fallback["marker"].read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise Refused("fallback_state_unavailable") from None
            if (document.get("container_id") != fallback["container_id"]
                    or type(document.get("started")) is not bool):
                raise Refused("fallback_state_unavailable")
            return True
        if fallback["process"].poll() is not None:
            raise Refused("fallback_process_failed")
        return False

    def cleanup(self, unit, identifier, injected):
        if not self.codespaces:
            return super().cleanup(unit, identifier, injected)
        if injected:
            state = self.inspect("php")
            if state["id"] != identifier:
                raise Refused("php_replaced_during_test_leave_fallback_for_original_id")
            if not base.same_running(state, identifier):
                if self.command(["/usr/bin/docker", "start", identifier], timeout=15).returncode:
                    raise Refused("manual_fallback_start_failed_guard_retained")
            if not base.same_running(self.inspect("php"), identifier):
                raise Refused("php_running_not_confirmed_guard_retained")
        fallback = self.fallbacks.get(unit)
        if fallback is None:
            return {"php_running": bool(injected), "fallback_timer_stopped": False}
        process = fallback["process"]
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        return {"php_running": bool(injected), "fallback_timer_stopped": process.poll() is not None,
                "scheduler": "supervisor"}

    def preflight(self, *, require_mode=False):
        self.operations_clear()
        self.claim_ready()
        health = self.json("/api/v1/health")
        if health.get("status") != "ok" or health.get("service") != "moodle-control-plane":
            raise Refused("control_plane_not_ready")
        controls = self.json("/api/v1/policies")
        policy = policy_snapshot(controls)
        worker = controls.get("worker", {})
        if worker.get("status") != "idle":
            raise Refused("automatic_worker_not_recent_and_idle")
        if require_mode:
            require_automatic(policy)
        scheduler = self.scheduler_ready()
        metrics = self.json("/api/v1/metrics/summary")
        self.metric_gate(metrics, time.time(), available=True)
        values = metrics["metrics"]
        if values["cpu_usage_percent"] >= 80 or values["memory_usage_percent"] >= 90 or values["disk_available_percent"] <= 10:
            raise ResourcePressure(metrics)
        decisions = self.json("/api/v1/decisions/summary")
        if decisions.get("snapshot_usable") is not True or decisions.get("policy_sha256") != base.POLICY_SHA:
            raise Refused("observer_or_decision_policy_not_ready")
        incidents = self.json("/api/v1/incidents?status=open&limit=100")
        if incidents.get("has_more") is not False or any(
            item["signal_key"] == "moodle_https" for item in incidents["items"]
        ):
            raise Refused("https_incident_already_open_or_list_incomplete")
        containers = {name: self.inspect(name) for name in ("php", "apache", "postgres")}
        if not all(base.same_running(item) for item in containers.values()) or containers["postgres"]["health"] != "healthy" or containers["php"]["offline_maintenance"]:
            raise Refused("containers_not_ready")
        if self.probe().get("success") is not True:
            raise Refused("initial_https_failed")
        return {"policy": policy, "worker": worker, "scheduler": scheduler,
                "api_version": health.get("version"),
                "automatic_ready": automatic_ready(policy), "metrics": metrics,
                "containers": containers, "previous_ids": [item["id"] for item in incidents["items"]]}

    def action_detail(self, incident_id, fault_at):
        listing = self.audit.listing(incident_id=incident_id, limit=100)
        if listing.get("has_more") is not False:
            raise Refused("remediation_audit_incomplete")
        items = [item for item in listing["items"] if item["started_at"] >= fault_at]
        if len(items) > 1:
            raise Refused("multiple_remediation_runs_for_test_incident")
        if not items or items[0]["status"] == "running":
            return None
        identifier = items[0]["id"]
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise Refused("remediation_run_id_invalid")
        return self.audit.detail(identifier)


def scenario(lab, report, save, *, inject=False, pause=time.sleep, clock=time.monotonic):
    unit = "pfe-auto-php-secours-" + uuid.uuid4().hex[:12]
    armed = injected = False
    php_id = None
    try:
        baseline = lab.preflight(require_mode=inject)
        report.update(status="ready", baseline=baseline,
                      operating_policy=baseline["policy"],
                      automatic_ready=baseline["automatic_ready"],
                      mode="automatic_fault_validation" if inject else "read_only_preflight")
        save()
        if not inject:
            return
        policy = baseline["policy"]
        php_id = baseline["containers"]["php"]["id"]
        lab.arm(unit, php_id)
        armed = True
        deadline = clock() + 225
        report["fallback_unit"] = unit
        latest = lab.preflight(require_mode=True)
        same_policy(policy, {"status": "available", "policy": latest["policy"]})
        if any(latest["containers"][name]["id"] != baseline["containers"][name]["id"] for name in baseline["containers"]):
            raise Refused("container_changed_before_fault")
        if clock() >= deadline or lab.fallback_used(unit):
            raise Refused("fallback_started_before_fault")
        report["fault_requested_at"] = time.time()
        save()
        injected = True
        stopped = lab.command(["/usr/bin/docker", "stop", "--time", "20", php_id], timeout=30)
        if stopped.returncode or lab.inspect("php")["state"]["Status"] != "exited":
            raise Refused("php_stop_not_confirmed")
        report.update(status="waiting_automatic_worker", php_stopped_at=time.time(),
                      https_during_fault=lab.probe())
        save()
        incident_id = None
        detail = None
        for _ in range(46):
            if clock() >= deadline or lab.fallback_used(unit):
                raise Refused("automatic_worker_wait_expired_or_fallback_started")
            same_policy(policy, lab.json("/api/v1/policies"))
            if incident_id is None:
                incident_id = base.candidate(lab.json("/api/v1/incidents?status=open&limit=100"),
                                             baseline["previous_ids"], report["fault_requested_at"])
                if incident_id is not None:
                    report["incident_id"] = incident_id
                    save()
            if incident_id is not None:
                detail = lab.action_detail(incident_id, report["fault_requested_at"])
                if detail is not None:
                    break
            pause(5)
        if detail is None:
            raise Refused("automatic_remediation_not_observed")
        report["audit"] = detail
        if lab.fallback_used(unit) or not verify_automatic_action(
            detail, php_id, incident_id, report["fault_requested_at"], policy
        ):
            raise Refused("automatic_remediation_not_proven")
        report["https_after"] = lab.probe()
        report["metrics_after"] = lab.json("/api/v1/metrics/summary")
        lab.metric_gate(report["metrics_after"], time.time(), available=True)
        if report["https_after"].get("success") is not True or not base.same_running(lab.inspect("php"), php_id):
            raise Refused("final_return_not_confirmed")
        report.update(status="automatic_recovery_validated", run_id=detail["id"],
                      incident_resolution="not_checked", manual_remediation_invoked=False,
                      elapsed_to_confirmation_seconds=round(time.time() - report["fault_requested_at"], 3))
    except KeyboardInterrupt:
        report.update(status="interrupted", reason="operator_interrupt")
    except Exception as exc:
        report.update(status="inconclusive" if injected else "blocked",
                      reason=str(exc)[:240], error_type=type(exc).__name__)
        if isinstance(exc, ResourcePressure):
            report["resource_check"] = exc.evidence
    finally:
        if armed:
            try:
                report["fallback_used"] = lab.fallback_used(unit)
                if report["fallback_used"] and report["status"] == "automatic_recovery_validated":
                    report.update(status="inconclusive", reason="fallback_used")
            except Exception as exc:
                report["fallback_state_error"] = str(exc)[:240]
                if report["status"] == "automatic_recovery_validated":
                    report.update(status="inconclusive", reason="fallback_state_unavailable")
            try:
                report["cleanup"] = lab.cleanup(unit, php_id, injected)
                if not report["cleanup"].get("fallback_timer_stopped"):
                    if report["status"] == "automatic_recovery_validated":
                        report.update(status="inconclusive", reason="cleanup_not_confirmed")
            except Exception as exc:
                report["cleanup"] = {"error": str(exc)[:240], "fallback_timer_retained": True}
                if report["status"] == "automatic_recovery_validated":
                    report.update(status="inconclusive", reason="cleanup_not_confirmed")
        report["finished_at"] = time.time()
        save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Arreter PHP sur le pilote et observer sa correction par le worker automatique.")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Lancer avec sudo et le Python de /opt/moodle-diagnostics/venv.")
    repo = Path.cwd()
    if not (repo / ".git").exists():
        parser.error("Se placer dans ~/moodle-infra.")
    os.umask(0o027)
    validation_root = repo / "ml/data/validation"
    validation_root.mkdir(mode=0o755, parents=True, exist_ok=True)
    identity = sudo_identity()
    if identity is not None and validation_root.stat().st_uid == 0:
        os.chown(validation_root, *identity)
    directory = validation_root / (
        "automatic_php_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True)
    output = directory / "report.json"
    report = {"status": "starting", "started_at": time.time(), "report_file": str(output),
              "semantics": "Un scenario sur le pilote ; aucune mesure de disponibilite generale ou de precision ML."}

    def save():
        temporary = output.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
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
        with Path("/var/lib/moodle-remediation/validation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            scenario(Lab(), report, save, inject=args.run)
    except Exception as exc:
        report.update(status="blocked", reason=str(exc)[:240], error_type=type(exc).__name__)
        save()
    handoff_report(directory, output, identity)
    keys = ("status", "report_file", "automatic_ready", "operating_policy", "incident_id", "run_id",
            "elapsed_to_confirmation_seconds", "fallback_used", "cleanup", "reason", "resource_check")
    print(json.dumps({k: report[k] for k in keys if k in report}, indent=2, ensure_ascii=False))
    return 0 if report["status"] in ("ready", "automatic_recovery_validated") else 2


if __name__ == "__main__":
    sys.exit(main())
