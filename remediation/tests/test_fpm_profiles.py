"""Fixtures sans Docker : autorisation, changement borne, preuve et retour arriere."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from diagnostics.policy import Blocked, POLICY_SHA, POLICY_VERSION
from pilotage.operating_policy import PolicyBlocked
from remediation import fpm_profiles as f
from remediation.policy import decision_for_correction
from remediation.store import RemediationStore

BASE = 1791645600.0
CONTENT = "[www]\npm = dynamic\npm.max_children = 5\npm.start_servers = 2\npm.min_spare_servers = 1\npm.max_spare_servers = 3\npm.status_path = /fpm-status\npm.status_listen = 9001\n"
RUN = "a" * 32


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def decision(now=BASE):
    return {"status": "available", "mode": "simulation", "snapshot_usable": True, "policy_sha256": POLICY_SHA,
        "decision": {"incident_id": 7, "signal_key": "php_pool_queue", "incident_status": "open",
            "latest_state": "triggered", "status": "recommendation", "policy_version": POLICY_VERSION,
            "policy_sha256": POLICY_SHA, "input_evaluated_at": iso(BASE), "valid_until": iso(BASE + 90),
            "decision_id": "d" * 64, "context": "policy-v1", "evidence": {"pool": "www", "listen_queue": 2,
                "active_processes": 5, "idle_processes": 0, "total_processes": 5, "scrape_age_seconds": 1},
            "recommended_action": {"id": "diagnose_php_pool", "kind": "diagnostic_recommendation", "executable": False}}}


def applications(now, *, busy=True, age=0):
    return {"source": "prometheus", "evaluated_at": iso(now), "services": {
        "php_fpm": {"status": "available", "service_reachable": True,
            "collection": {"status": "up", "scrape_age_seconds": age}, "metrics": {
                "listen_queue": 2 if busy else 0, "idle_processes": 0 if busy else 2,
                "active_processes": 5 if busy else 2, "total_processes": 5 if busy else 4}},
        "postgresql": {"status": "available", "service_reachable": True,
            "collection": {"status": "up", "scrape_age_seconds": 0},
            "metrics": {"server_connections": 10, "max_connections": 100}}}}


class FakeRuntime:
    def __init__(self, p, clock):
        self.p = p; self.clock = clock; self.reloads = 0; self.fail_benefit = False
        self.fail_memory = False; self.fail_rollback = False; self.stale_scrape = False
        self.validated = []; self.starts = [int(BASE-100)]
        self.services = {name: {"id": letter*64, "health": "healthy", "offline_maintenance": False,
            "state": {"Status": "running", "Running": True, "Restarting": False, "Paused": False,
                      "OOMKilled": False, "Dead": False}} for name,letter in (("php","a"),("apache","b"),("postgres","c"))}
    def clear(self): pass
    def master(self, container): pass
    def effective(self, container, limit):
        expected = "pm.max_children = " + str(limit)
        if expected not in self.p.target.read_text(): raise Blocked("effective_pool_limit_not_confirmed")
    def inspect(self): return copy.deepcopy(self.services)
    def mount(self, container, target): return f.sha(self.p.target.read_bytes())
    def validate(self, container, content=None): self.validated.append(content)
    def budget(self, container):
        if self.fail_memory: raise Blocked("insufficient_memory_budget_for_profile")
        return {"available_bytes": 2*f.MIB*1024, "additional_budget_bytes": 1376*f.MIB}
    def probe(self): return {"success": True, "http_status": 200, "tls_validated": True}
    def reload(self, container):
        self.reloads += 1
        if self.fail_rollback and self.reloads > 1: raise Blocked("php_fpm_reload_failed")
        self.clock["now"] += 2; self.starts.append(int(self.clock["now"]))
    def pool(self, container):
        busy = self.reloads == 0 or self.fail_benefit and self.reloads == 1
        active = (5 if self.reloads == 0 else 8) if busy else 2
        return {"pool": "www", "start_time": self.starts[-1], "listen_queue": 2 if busy else 0,
                "active_processes": active, "idle_processes": 0 if busy else 2, "total_processes": active if busy else 4}
    def read(self, path):
        now = self.clock["now"]
        if path.startswith("/api/v1/decisions/"): return decision(now)
        if path.endswith("/applications"):
            doc = applications(now, busy=self.reloads == 0 or self.fail_benefit)
            if self.stale_scrape and self.reloads: doc["services"]["php_fpm"]["collection"]["scrape_age_seconds"] = now-BASE+1
            return doc
        return {"source": "prometheus", "evaluated_at": iso(now),
                "collection": {job: {"status": "up", "scrape_age_seconds": 0} for job in ("linux","moodle_https")},
                "metrics": {"cpu_usage_percent": 20, "memory_usage_percent": 30,
                            "disk_available_percent": 40, "moodle_available": True}}


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name); self.target = self.parent / "zz-moodle.conf"
        self.target.write_text(CONTENT); self.target.chmod(0o644)
        self.p = f.Profiles(self.parent, target=self.target); self.clock = {"now": BASE}
        self.rt = FakeRuntime(self.p, self.clock)
        self.config = {"data_dir": self.tmp.name, "policy_required": True, "php_profiles_enabled": True}
        self.ps = Mock(); self.ps.view.return_value = {"policy": {"revision": 2, "mode": "observation"}}
        for item in (patch.object(f, "PROFILE_OWNER_UID", os.getuid()), patch.object(f.os, "chown"),
                     patch.object(f.os, "fchown"), patch.object(f.time, "time", lambda: self.clock["now"]),
                     patch.dict(os.environ, {"PILOTAGE_DATABASE_CONFIG": ""})):
            item.start(); self.addCleanup(item.stop)
        f.prepare(self.config, runtime=self.rt, profiles=self.p, policy_store=self.ps)
        self.store = RemediationStore(self.tmp.name); self.store.initialize(); self.store.start(RUN, 7, BASE)
        self.expected = decision_for_correction(decision(), 7, BASE)
        self.expected["operating_policy"] = {"revision": 3, "sha256": "e"*64}
        self.expected["execution_mode"] = "automatic_guarded"
        def authorize(document, policy, *, reserve, **kwargs):
            self.assertEqual(self.target.read_text(), CONTENT)
            self.assertIsNone(reserve())
            return {"policy": policy, "trigger_mode": "automatic", "approval_id": None}
        self.ps.dispatch.side_effect = authorize
    def run_profile(self, **kwargs):
        return f.apply(self.config, 7, RUN, self.expected, runtime=self.rt, profiles=self.p,
            policy_store=self.ps, store=self.store, pause=lambda n: self.clock.update(now=self.clock["now"]+n), **kwargs)

    def test_preparation_validates_both_fixed_profiles_without_changing_production(self):
        self.assertEqual(self.target.read_text(), CONTENT); self.assertEqual(self.rt.reloads, 0)
        self.assertIn("pm.max_children = 8", self.rt.validated[1])
        catalog_bytes = (self.p.root/"catalog.json").read_bytes()
        self.assertFalse(f.prepare(self.config, runtime=self.rt, profiles=self.p)["changed"])
        self.assertEqual((self.p.root/"catalog.json").read_bytes(), catalog_bytes)

    def test_first_preparation_requires_observation_before_enlarging_action_scope(self):
        (self.p.root/"catalog.json").unlink(); self.ps.view.return_value["policy"]["mode"] = "automatic"
        with self.assertRaisesRegex(Blocked, "set_observation"):
            f.prepare(self.config, runtime=self.rt, profiles=self.p, policy_store=self.ps)
        self.assertEqual(self.rt.reloads, 0); self.assertEqual(self.target.read_text(), CONTENT)

    def test_verified_change_preserves_bind_inode_and_audits_the_new_action(self):
        inode = self.target.stat().st_ino; result = self.run_profile()
        self.assertEqual((result["status"], result["recovered"]), ("completed", True), result)
        self.assertEqual(self.target.stat().st_ino, inode); self.assertEqual(self.rt.reloads, 1)
        self.assertIn("pm.max_children = 8", self.target.read_text())
        self.assertGreaterEqual(len({s["php_scrape_at"] for s in result["checks"]["profile_samples"] if s["confirmed"]}), 3)
        self.assertFalse((self.p.root/"pending.json").exists())
        self.assertEqual(self.store.detail(RUN)["action_reservation"]["action_id"], f.PROFILE_ACTION)

    def test_old_policy_confirmation_cannot_authorize_new_profiles(self):
        self.expected["operating_policy"]["revision"] = 2
        result = self.run_profile(); self.assertEqual(result["reason"], "new_policy_confirmation_required_for_profiles")
        self.assertEqual(self.target.read_text(), CONTENT); self.ps.dispatch.assert_not_called()
        self.assertIsNone(self.store.detail(RUN)["action_reservation"])

    def test_suspended_policy_or_missing_evaluation_approval_never_writes(self):
        for reason in ("policy_suspended", "evaluation_approval_required"):
            self.ps.dispatch.side_effect = PolicyBlocked(reason)
            result = self.run_profile(); self.assertEqual(result["reason"], reason)
            self.assertEqual(self.target.read_text(), CONTENT); self.assertEqual(self.rt.reloads, 0)

    def test_memory_shortage_is_blocked_before_reservation_and_reload(self):
        self.rt.fail_memory = True; result = self.run_profile()
        self.assertEqual(result["status"], "blocked"); self.assertFalse(result["action_dispatched"])
        self.assertEqual(self.target.read_text(), CONTENT); self.ps.dispatch.assert_not_called()

    def test_failed_benefit_restores_config_and_confirms_a_second_reload(self):
        self.rt.fail_benefit = True; result = self.run_profile(window=60)
        self.assertEqual(result["status"], "failed"); self.assertFalse(result["recovered"])
        self.assertTrue(result["rollback"]["confirmed"], result)
        self.assertEqual(self.target.read_text(), CONTENT); self.assertEqual(self.rt.reloads, 2)
        self.assertFalse((self.p.root/"pending.json").exists())

    def test_failed_rollback_preserves_pending_record_and_escalates(self):
        self.rt.fail_benefit = True; self.rt.fail_rollback = True; result = self.run_profile()
        self.assertFalse(result["rollback"]["confirmed"])
        self.assertTrue((self.p.root/"pending.json").exists())
        self.assertEqual(json.loads((self.p.root/"status.json").read_text())["state"], "review_required")

    def test_old_or_missing_metrics_cannot_confirm_success(self):
        self.rt.stale_scrape = True; result = self.run_profile()
        self.assertFalse(result["recovered"]); self.assertTrue(result["rollback"]["confirmed"])

    def test_previous_incomplete_change_and_configuration_drift_are_blocked(self):
        (self.p.root/"pending.json").write_text("{}"); result = self.run_profile()
        self.assertEqual(result["reason"], "previous_profile_change_requires_review")
        (self.p.root/"pending.json").unlink(); self.target.write_text(CONTENT+"; external change\n")
        result = self.run_profile(); self.assertEqual(result["reason"], "profile_configuration_drift")
        self.assertEqual(self.rt.reloads, 0)

    def test_symlink_or_writable_configuration_is_not_accepted(self):
        self.target.chmod(0o666)
        with self.assertRaises(Blocked): self.p.current(self.p.load())
        self.target.chmod(0o644); alternative = self.parent/"alternate.conf"
        self.target.rename(alternative); self.target.symlink_to(alternative)
        with self.assertRaises(OSError): self.p.current(self.p.load())

    def test_finite_pool_counts_postgres_capacity_and_resource_margins_are_required(self):
        good = applications(BASE)
        for change in ("stale", "inconsistent", "postgres", "nonfinite"):
            d = copy.deepcopy(good)
            if change == "stale": d["services"]["php_fpm"]["collection"]["scrape_age_seconds"] = 31
            if change == "inconsistent": d["services"]["php_fpm"]["metrics"]["total_processes"] = 6
            if change == "postgres": d["services"]["postgresql"]["metrics"]["server_connections"] = 70
            if change == "nonfinite": d["services"]["php_fpm"]["metrics"]["listen_queue"] = float("nan")
            with self.assertRaises(Blocked): f.applications_gate(d, BASE)
        metrics = self.rt.read("/api/v1/metrics/summary"); metrics["metrics"]["cpu_usage_percent"] = 70
        with self.assertRaises(Blocked): f.resource_gate(metrics, BASE)

    def test_codespaces_loopback_http_is_accepted_only_as_declared_tls_termination(self):
        self.config.update(probe_scheme="http", proxy_tls_terminated=True, ca_file="")
        self.rt.probe = Mock(return_value={"success":True,"http_status":200,"tls_validated":False,
                                           "loopback_validated":True,"scheme":"http"})
        result=self.run_profile()
        self.assertEqual(result["status"],"completed",result)

        self.target.write_text(CONTENT)
        (self.p.root/"status.json").unlink(missing_ok=True)
        (self.p.root/"catalog.json").unlink(missing_ok=True)
        self.rt.reloads=0; self.rt.starts=[int(BASE-100)]
        f.prepare(self.config,runtime=self.rt,profiles=self.p,policy_store=self.ps)
        self.config["proxy_tls_terminated"]=False
        result=self.run_profile()
        self.assertEqual(result["reason"],"moodle_access_not_confirmed")


class RuntimeGuardTests(unittest.TestCase):
    def test_effective_pool_limit_is_checked_after_all_includes(self):
        rt = f.Runtime({})
        output = "NOTICE: [global]\nNOTICE: [www]\nNOTICE: pm = dynamic\nNOTICE: pm.max_children = 8\nNOTICE: [status]\nNOTICE: pm.max_children = 1\n"
        with patch.object(rt,'execute',return_value=SimpleNamespace(returncode=0,stdout='',stderr=output)):
            rt.effective('a'*64,8)
            with self.assertRaisesRegex(Blocked,'effective_pool_limit'): rt.effective('a'*64,5)

    def test_explicit_memory_cap_must_cover_the_whole_pool(self):
        rt = f.Runtime({})
        answers = [SimpleNamespace(returncode=0,stdout='256M'), SimpleNamespace(returncode=0,stdout=str(512*f.MIB))]
        with patch.object(rt,'execute',side_effect=answers),patch.object(Path,'read_text',return_value='MemAvailable: 4194304 kB\n'):
            with self.assertRaisesRegex(Blocked,'insufficient_memory_budget'): rt.budget('a'*64)
