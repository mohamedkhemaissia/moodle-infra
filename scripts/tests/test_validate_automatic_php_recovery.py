import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "automatic_validator", Path(__file__).resolve().parents[1] / "validate_automatic_php_recovery.py")
v = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(v)
GUARD_SPEC = importlib.util.spec_from_file_location(
    "php_fallback_guard", Path(__file__).resolve().parents[1] / "php_fallback_guard.py")
guard = importlib.util.module_from_spec(GUARD_SPEC)
GUARD_SPEC.loader.exec_module(guard)
PHP_ID = "a" * 64


def policy(*, mode="automatic", suspended=False, evaluation_context=False, revision=2):
    value = dict(revision=revision, mode=mode, suspended=suspended, evaluation_context=evaluation_context)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return {**value, "sha256": hashlib.sha256(encoded.encode()).hexdigest()}


class FakeLab:
    def __init__(self, **fields):
        self.policy = policy(**fields)
        self.armed = self.stopped = self.started = self.cleaned = self.used = False
        self.changed_policy = self.changed_container = self.interrupt = False
        self.no_incident = self.no_audit = self.manual = self.bad_audit = False
        self.arm_fails = self.cleanup_fails = self.fallback_error = False
        self.calls = self.reads = 0
        self.commands = []

    def preflight(self, *, require_mode=False):
        self.calls += 1
        if require_mode:
            v.require_automatic(self.policy)
        services = {name: {"id": PHP_ID, "state": {"Running": True, "Status": "running",
                    "Restarting": False, "Paused": False, "OOMKilled": False, "Dead": False}}
                    for name in ("php", "apache", "postgres")}
        if self.changed_container and self.calls == 2:
            services["php"]["id"] = "b" * 64
        return {"policy": self.policy.copy(), "containers": services, "previous_ids": [1],
                "automatic_ready": v.automatic_ready(self.policy)}

    def arm(self, unit, identifier):
        if self.arm_fails:
            raise v.Refused("timer_unavailable")
        self.armed = True

    def command(self, argv, timeout=5):
        if not self.armed:
            raise AssertionError("Fault before fallback")
        self.commands.append(argv)
        self.stopped = True
        return subprocess.CompletedProcess(argv, 0, "", "")

    def inspect(self, _):
        return {"id": PHP_ID, "state": {"Running": not self.stopped or self.started,
            "Status": "running" if not self.stopped or self.started else "exited",
            "Restarting": False, "Paused": False, "OOMKilled": False, "Dead": False}}

    def fallback_used(self, _):
        if self.fallback_error and self.started:
            raise v.Refused("fallback_unavailable")
        return self.used

    def probe(self):
        return {"success": not self.stopped or self.started}

    def json(self, path):
        if path == "/api/v1/policies":
            self.reads += 1
            if self.changed_policy:
                return {"status": "available", "policy": policy(revision=3)}
            return {"status": "available", "policy": self.policy.copy()}
        if path.startswith("/api/v1/incidents?"):
            stamp = v.datetime.fromtimestamp(time.time(), v.timezone.utc).isoformat()
            return {"has_more": False, "items": [] if self.no_incident else [
                {"id": 2, "signal_key": "moodle_https", "status": "open",
                 "latest_state": "triggered", "first_observed_at": stamp}]}
        return {}

    def action_detail(self, identifier, fault_at):
        if self.interrupt:
            raise KeyboardInterrupt
        if self.no_audit:
            return None
        self.started = True
        mode = "manual_guarded" if self.manual else "automatic_guarded"
        return {"id": "c" * 32, "incident_id": identifier, "action_id": "start_php_stopped",
            "status": "completed", "started_at": fault_at, "finished_at": time.time(),
            "runner_status": "successful", "runner_rc": 0,
            "decision": {"execution_mode": mode, "operating_policy": self.policy.copy()},
            "action_reservation": {"container_id": "b" * 64 if self.bad_audit else PHP_ID,
                "incident_id": identifier, "claimed_at": time.time(), "run_id": "c" * 32,
                "action_id": "start_php_stopped"},
            "report": {"action_dispatched": True, "recovered": True, "mode": mode,
                "reason": "https_and_new_probe_confirmed", "operating_authorization": {
                    "trigger_mode": "manual" if self.manual else "automatic",
                    "policy": self.policy.copy(), "approval_id": None}}}

    def metric_gate(self, *args, **kwargs):
        pass

    def cleanup(self, unit, identifier, injected):
        self.cleaned = True
        self.started = True
        return {"php_running": injected, "fallback_timer_stopped": not self.cleanup_fails}

    def worker(self, _):
        raise AssertionError("Validator must never launch remediation")


class ScenarioTests(unittest.TestCase):
    def run_case(self, lab, *, inject=True):
        report, saved, ticks = {}, [], [0]
        def pause(seconds):
            ticks[0] += seconds
        v.scenario(lab, report, lambda: saved.append(copy.deepcopy(report)), inject=inject,
                   pause=pause, clock=lambda: ticks[0])
        return report

    def test_default_preflight_preserves_inactive_mode_and_never_stops_php(self):
        lab = FakeLab(mode="assisted", suspended=True, evaluation_context=True)
        result = self.run_case(lab, inject=False)
        self.assertEqual(result["status"], "ready")
        self.assertFalse(result["automatic_ready"])
        self.assertFalse(lab.armed or lab.stopped or lab.commands)

    def test_success_requires_automatic_audit_and_no_manual_invocation(self):
        lab = FakeLab()
        result = self.run_case(lab)
        self.assertEqual(result["status"], "automatic_recovery_validated")
        self.assertFalse(result["manual_remediation_invoked"])
        self.assertEqual(lab.commands, [["/usr/bin/docker", "stop", "--time", "20", PHP_ID]])
        self.assertTrue(lab.cleaned)

    def test_observation_assisted_suspension_and_evaluation_prevent_fault(self):
        for fields in ({"mode": "observation"}, {"mode": "assisted"},
                       {"suspended": True}, {"evaluation_context": True}):
            with self.subTest(**fields):
                lab = FakeLab(**fields)
                self.assertEqual(self.run_case(lab)["status"], "blocked")
                self.assertFalse(lab.armed or lab.stopped)

    def test_changed_container_after_arm_prevents_fault(self):
        lab = FakeLab(); lab.changed_container = True
        self.assertEqual(self.run_case(lab)["status"], "blocked")
        self.assertTrue(lab.cleaned)
        self.assertFalse(lab.stopped)

    def test_failure_to_arm_prevents_fault(self):
        lab = FakeLab(); lab.arm_fails = True
        self.assertEqual(self.run_case(lab)["status"], "blocked")
        self.assertFalse(lab.stopped)

    def test_policy_change_during_wait_aborts_and_restores_php(self):
        lab = FakeLab(); lab.changed_policy = True
        result = self.run_case(lab)
        self.assertEqual(result["reason"], "operating_policy_changed_during_validation")
        self.assertTrue(lab.started and lab.cleaned)

    def test_missing_incident_or_audit_cannot_prove_recovery(self):
        for field in ("no_incident", "no_audit"):
            lab = FakeLab(); setattr(lab, field, True)
            self.assertEqual(self.run_case(lab)["status"], "inconclusive")
            self.assertTrue(lab.cleaned)

    def test_manual_recovery_and_wrong_container_audit_are_not_automatic_proof(self):
        for field in ("manual", "bad_audit"):
            lab = FakeLab(); setattr(lab, field, True)
            self.assertEqual(self.run_case(lab)["reason"], "automatic_remediation_not_proven")
            self.assertTrue(lab.cleaned)

    def test_fallback_while_waiting_is_never_reported_as_success(self):
        lab = FakeLab(); lab.used = True
        result = self.run_case(lab)
        self.assertNotEqual(result["status"], "automatic_recovery_validated")
        self.assertFalse(lab.stopped)
        self.assertTrue(lab.cleaned)

    def test_unknown_fallback_or_failed_cleanup_invalidates_success(self):
        for field in ("fallback_error", "cleanup_fails"):
            lab = FakeLab(); setattr(lab, field, True)
            self.assertEqual(self.run_case(lab)["status"], "inconclusive")
            self.assertTrue(lab.cleaned)

    def test_interrupt_restores_php(self):
        lab = FakeLab(); lab.interrupt = True
        self.assertEqual(self.run_case(lab)["status"], "interrupted")
        self.assertTrue(lab.started and lab.cleaned)


class ProofTests(unittest.TestCase):
    def test_report_handoff_only_uses_valid_sudo_identity(self):
        with patch.dict(v.os.environ, {"SUDO_UID": "1000", "SUDO_GID": "1001"}, clear=True):
            self.assertEqual(v.sudo_identity(), (1000, 1001))
        for values in ({}, {"SUDO_UID": "0", "SUDO_GID": "0"},
                       {"SUDO_UID": "root", "SUDO_GID": "1001"}):
            with self.subTest(values=values), patch.dict(v.os.environ, values, clear=True):
                self.assertIsNone(v.sudo_identity())

    def test_fallback_marker_is_confined_to_private_runtime_directory(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            guard, "RUNTIME_DIR", Path(directory)
        ):
            expected = Path(directory) / ("pfe-auto-php-secours-" + "a" * 12 + ".json")
            self.assertEqual(guard.confined_marker(str(expected)), expected)
            with self.assertRaises(ValueError):
                guard.confined_marker(str(Path(directory).parent / expected.name))

    def test_scheduler_matches_codespaces_and_reference_vm(self):
        lab = v.Lab.__new__(v.Lab)
        lab.codespaces = True
        lab.command = lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, "moodle-remediation-worker RUNNING pid 42, uptime 0:01:00\n", "")
        self.assertEqual(lab.scheduler_ready(), "supervisor")
        lab.codespaces = False
        lab.command = lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "active\n", "")
        self.assertEqual(lab.scheduler_ready(), "systemd")

    def test_codespaces_fallback_guard_is_armed_observed_and_stopped(self):
        class Process:
            def __init__(self): self.returncode = None
            def poll(self): return self.returncode
            def terminate(self): self.returncode = -15
            def kill(self): self.returncode = -9
            def wait(self, timeout=None): return self.returncode

        lab = v.Lab.__new__(v.Lab)
        lab.codespaces = True
        lab.fallbacks = {}
        unit = "pfe-auto-php-secours-" + "b" * 12
        process = Process()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(v, "FALLBACK_DIR", Path(directory)), \
                patch.object(v.subprocess, "Popen", return_value=process) as popen:
            lab.arm(unit, PHP_ID)
            self.assertFalse(lab.fallback_used(unit))
            fallback = lab.fallbacks[unit]
            fallback["marker"].write_text(json.dumps(
                {"container_id": PHP_ID, "started": True}), encoding="utf-8")
            process.returncode = 0
            self.assertTrue(lab.fallback_used(unit))
            result = lab.cleanup(unit, PHP_ID, False)
            self.assertTrue(result["fallback_timer_stopped"])
            self.assertEqual(result["scheduler"], "supervisor")
            self.assertIn("php_fallback_guard.py", " ".join(popen.call_args.args[0]))

    def test_resource_report_identifies_each_limit_and_boundary(self):
        for key, value in (("cpu_usage_percent", 80), ("memory_usage_percent", 90),
                           ("disk_available_percent", 10)):
            metrics = {"evaluated_at": "2026-10-10T14:59:00Z", "metrics": {
                "cpu_usage_percent": 10, "memory_usage_percent": 30, "disk_available_percent": 40}}
            metrics["metrics"][key] = value
            with self.subTest(metric=key):
                pressure = v.ResourcePressure(metrics)
                self.assertEqual(pressure.evidence["blocked_resources"], [key])
                self.assertEqual(pressure.evidence["measured_percent"][key], value)
                self.assertEqual(pressure.evidence["evaluated_at"], metrics["evaluated_at"])

    def test_pressure_is_saved_before_any_fault_or_timer_is_armed(self):
        lab = FakeLab()
        metrics = {"evaluated_at": "2026-10-10T14:59:00Z", "metrics": {
            "cpu_usage_percent": 91.5, "memory_usage_percent": 25, "disk_available_percent": 39}}
        lab.preflight = lambda **kwargs: (_ for _ in ()).throw(v.ResourcePressure(metrics))
        report, saved = {}, []
        v.scenario(lab, report, lambda: saved.append(copy.deepcopy(report)), inject=True)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["resource_check"]["blocked_resources"], ["cpu_usage_percent"])
        self.assertEqual(saved[-1]["resource_check"]["measured_percent"]["cpu_usage_percent"], 91.5)
        self.assertFalse(lab.armed or lab.stopped or lab.commands)

    def test_preflight_accepts_current_api_and_preserves_assisted_suspended_policy(self):
        lab = v.Lab.__new__(v.Lab)
        lab.codespaces = False
        lab.operations_clear = lab.claim_ready = lambda: None
        lab.command = lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "active\n", "")
        lab.metric_gate = lambda *args, **kwargs: None
        lab.probe = lambda: {"success": True}
        lab.inspect = lambda name: {**FakeLab().inspect(name),
                                   "health": "healthy" if name == "postgres" else None,
                                   "offline_maintenance": False}
        controls = {"status": "available", "policy": policy(mode="assisted", suspended=True),
                    "worker": {"status": "idle"}}
        def reader(path):
            return {"/api/v1/health": {"status": "ok", "service": "moodle-control-plane", "version": "0.14.0"},
                    "/api/v1/policies": controls,
                    "/api/v1/metrics/summary": {"metrics": {"cpu_usage_percent": 10, "memory_usage_percent": 30, "disk_available_percent": 40}},
                    "/api/v1/decisions/summary": {"snapshot_usable": True, "policy_sha256": v.base.POLICY_SHA},
                    "/api/v1/incidents?status=open&limit=100": {"has_more": False, "items": []}}[path]
        lab.json = reader
        value = lab.preflight()
        self.assertEqual(value["api_version"], "0.14.0")
        self.assertFalse(value["automatic_ready"])
        with self.assertRaises(v.Refused):
            lab.preflight(require_mode=True)

    def test_bad_policy_hash_and_non_boolean_flags_are_rejected(self):
        for key, value in (("sha256", "f" * 64), ("suspended", 0)):
            changed = policy(); changed[key] = value
            with self.subTest(key=key), self.assertRaises(v.Refused):
                v.policy_snapshot({"status": "available", "policy": changed})

    def test_revision_change_manual_authorization_and_missing_proof_are_rejected(self):
        lab = FakeLab()
        detail = lab.action_detail(2, time.time() - 1)
        self.assertTrue(v.verify_automatic_action(detail, PHP_ID, 2, detail["started_at"], lab.policy))
        for field, value in (("mode", "manual_guarded"), ("operating_authorization", {})):
            altered = copy.deepcopy(detail); altered["report"][field] = value
            self.assertFalse(v.verify_automatic_action(altered, PHP_ID, 2, detail["started_at"], lab.policy))
        self.assertFalse(v.verify_automatic_action(detail, PHP_ID, 2, detail["started_at"], policy(revision=3)))


if __name__ == "__main__":
    unittest.main()
