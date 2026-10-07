import copy
import importlib.util
import subprocess
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("validator", Path(__file__).resolve().parents[1] / "validate_php_recovery.py")
v = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v)

PHP_ID = "a" * 64


def container(name, identifier=None):
    return {"id": identifier or PHP_ID, "service": name, "health": "healthy" if name == "postgres" else None,
            "offline_maintenance": False,
            "state": {"Status": "running", "Running": True, "Restarting": False,
                      "Paused": False, "OOMKilled": False, "Dead": False}}


class Clock:
    value = 0
    def now(self):
        return self.value
    def pause(self, seconds):
        self.value += seconds


class FakeLab:
    blocked = v.Refused
    def __init__(self):
        self.stopped = False
        self.started = False
        self.armed = False
        self.cleaned = False
        self.workers = []
        self.commands = []
        self.listed = 0
        self.used = False
        self.fallback_error = False
        self.incident_available = True
        self.preflight_calls = 0
        self.preflight_change = False
        self.interrupt = False
        self.bad_audit = False
        self.arm_fails = False

    def preflight(self):
        self.preflight_calls += 1
        services = {name: container(name) for name in ("php", "apache", "postgres")}
        if self.preflight_change and self.preflight_calls == 2:
            services["php"]["id"] = "b" * 64
        return {"containers": services, "previous_ids": [1], "metrics": {}}

    def arm(self, unit, identifier):
        if self.arm_fails:
            raise v.Refused("timer_unavailable")
        self.armed = True

    def command(self, args, timeout=5):
        self.commands.append(args)
        if args[:3] == ["/usr/bin/docker", "stop", "--time"]:
            if not self.armed:
                raise AssertionError("Fault before fallback")
            self.stopped = True
        return subprocess.CompletedProcess(args, 0, "", "")

    def inspect(self, name):
        result = container(name)
        if name == "php" and self.stopped and not self.started:
            result["state"].update(Status="exited", Running=False)
        return result

    def probe(self):
        return {"success": not self.stopped or self.started}

    def fallback_used(self, unit):
        if self.fallback_error and self.workers:
            raise v.Refused("fallback_metadata_missing")
        return self.used

    def json(self, path):
        if path.startswith("/api/v1/incidents?"):
            self.listed += 1
            point = datetime.fromtimestamp(time.time(), timezone.utc).isoformat().replace("+00:00", "Z")
            items = [{"id": 2, "signal_key": "moodle_https", "status": "open", "latest_state": "triggered", "first_observed_at": point}] if self.incident_available and self.listed >= 2 else []
            return {"items": items, "has_more": False}
        if path.startswith("/api/v1/decisions/"):
            return {"decision": {"incident_id": 2}}
        if path.startswith("/api/v1/remediations/"):
            return {"status": "completed", "incident_id": 2,
                    "action_reservation": {"container_id": "b" * 64 if self.bad_audit else PHP_ID, "incident_id": 2, "claimed_at": time.time()},
                    "report": {"action_dispatched": True, "recovered": True, "reason": "https_and_new_probe_confirmed"}}
        return {}

    def decision_for_php(self, document, identifier, now):
        return {}

    def metric_gate(self, document, now, *, available):
        return now

    def worker(self, identifier):
        self.workers.append(identifier)
        if self.interrupt:
            raise KeyboardInterrupt
        self.started = True
        return {"exit_code": 0, "result": {"status": "completed", "run_id": "c" * 32}}

    def cleanup(self, unit, identifier, injected):
        self.cleaned = True
        if injected:
            self.started = True
        return {"php_running": injected, "fallback_timer_stopped": True}


class ScenarioTests(unittest.TestCase):
    def execute(self, lab, inject=True):
        report = {}
        saved = []
        clock = Clock()
        v.scenario(lab, report, lambda: saved.append(copy.deepcopy(report)), inject=inject,
                   pause=clock.pause, clock=clock.now)
        return report, saved

    def test_default_preflight_does_not_arm_or_stop(self):
        lab = FakeLab()
        report, saved = self.execute(lab, inject=False)
        self.assertEqual(report["status"], "ready")
        self.assertFalse(lab.armed or lab.stopped or lab.workers)

    def test_success_requires_new_incident_fixed_worker_and_durable_action_proof(self):
        lab = FakeLab()
        report, saved = self.execute(lab)
        self.assertEqual(report["status"], "recovery_validated")
        self.assertEqual(lab.workers, [2])
        self.assertEqual(lab.commands, [["/usr/bin/docker", "stop", "--time", "20", PHP_ID]])
        self.assertTrue(lab.cleaned)
        self.assertFalse(report["fallback_used"])
        self.assertIn("fault_requested_at", saved[1])

    def test_failed_fallback_arm_prevents_fault(self):
        lab = FakeLab()
        lab.arm_fails = True
        report, _ = self.execute(lab)
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(lab.stopped or lab.workers)

    def test_changed_container_after_arm_prevents_stop_and_cleans_timer(self):
        lab = FakeLab()
        lab.preflight_change = True
        report, _ = self.execute(lab)
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(lab.stopped)
        self.assertTrue(lab.cleaned)

    def test_no_incident_timeout_restores_php_and_never_runs_worker(self):
        lab = FakeLab()
        lab.incident_available = False
        report, _ = self.execute(lab)
        self.assertEqual(report["status"], "inconclusive")
        self.assertTrue(lab.started and lab.cleaned)
        self.assertFalse(lab.workers)

    def test_audit_for_another_container_cannot_validate_recovery(self):
        lab = FakeLab()
        lab.bad_audit = True
        report, _ = self.execute(lab)
        self.assertEqual(report["status"], "inconclusive")
        self.assertTrue(lab.cleaned)

    def test_interrupt_preserves_report_and_restores_php(self):
        lab = FakeLab()
        lab.interrupt = True
        report, saved = self.execute(lab)
        self.assertEqual(report["status"], "interrupted")
        self.assertTrue(lab.started and lab.cleaned)
        self.assertEqual(saved[-1]["status"], "interrupted")

    def test_unknown_fallback_metadata_still_runs_cleanup(self):
        lab = FakeLab()
        lab.fallback_error = True
        report, _ = self.execute(lab)
        self.assertEqual(report["status"], "inconclusive")
        self.assertTrue(lab.cleaned)

    def test_executed_fallback_is_not_reported_as_remediation_success(self):
        lab = FakeLab()
        original = lab.worker
        def worker(identifier):
            result = original(identifier)
            lab.used = True
            return result
        lab.worker = worker
        report, _ = self.execute(lab)
        self.assertEqual(report["status"], "inconclusive")
        self.assertTrue(report["fallback_used"])


class GuardTests(unittest.TestCase):
    def test_existing_incident_is_never_selected_and_incomplete_listing_is_rejected(self):
        point = datetime.fromtimestamp(1700000000, timezone.utc).isoformat().replace("+00:00", "Z")
        item = {"id": 1, "signal_key": "moodle_https", "status": "open", "latest_state": "triggered", "first_observed_at": point}
        self.assertIsNone(v.candidate({"items": [item], "has_more": False}, [1], 1700000000))
        with self.assertRaises(v.Refused):
            v.candidate({"items": [item], "has_more": True}, [], 1700000000)

    def test_cpu_margin_is_required_before_fault(self):
        lab = v.Lab.__new__(v.Lab)
        lab.operations_clear = lambda: None
        lab.claim_ready = lambda: None
        lab.metric_gate = lambda *a, **kw: None
        health = {"status": "ok", "service": "moodle-control-plane", "version": "0.7.0"}
        lab.json = lambda path: health if path.endswith("health") else {"metrics": {"cpu_usage_percent": 80, "memory_usage_percent": 25, "disk_available_percent": 40}}
        with self.assertRaisesRegex(v.Refused, "resources_not_ready"):
            lab.preflight()

    def test_cleanup_keeps_timer_when_php_cannot_be_restarted(self):
        lab = v.Lab.__new__(v.Lab)
        stopped = container("php")
        stopped["state"].update(Status="exited", Running=False)
        lab.inspect = lambda _: stopped
        commands = []
        def command(args, timeout=5):
            commands.append(args)
            return subprocess.CompletedProcess(args, 1, "", "")
        lab.command = command
        with self.assertRaisesRegex(v.Refused, "timer_retained"):
            lab.cleanup("test", PHP_ID, True)
        self.assertEqual(commands, [["/usr/bin/docker", "start", PHP_ID]])


if __name__ == "__main__":
    unittest.main()
