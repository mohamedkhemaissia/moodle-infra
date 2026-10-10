import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from fastapi.testclient import TestClient
from app.main import create_app
from app.php_profiles import read_status
from app.incident_signals import observations
from app.incidents import DEFINITIONS, IncidentStore
from app.decisions import summarize, policy_document


def snapshot():
    return {"collection": {job: {"status": "up", "scrape_age_seconds": 1} for job in ("linux","moodle_https")},
        "metrics": {"cpu_usage_percent": 20, "memory_usage_percent": 30, "disk_available_percent": 40,
            "moodle_available": True, "moodle_http_status_code": 200, "moodle_probe_duration_ms": 90}}


def application(queue=2, idle=0, age=1):
    return {"services": {"php_fpm": {"status": "available", "service_reachable": True,
        "collection": {"status": "up", "scrape_age_seconds": age}, "metrics": {
            "active_processes": 5, "idle_processes": idle, "total_processes": 5+idle,
            "listen_queue": queue, "max_children_reached": 10000}}}}


class ProfileAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/"php-profiles"; self.root.mkdir()
        self.value = {"format": "php-fpm-profile-status-v1", "recorded_at": time.time()-10,
            "state": "prepared", "recorded_profile": "normal", "activation_revision": 2,
            "run_id": None, "profiles": [{"id": k, "max_children": v, "sha256": c*64}
                for k,v,c in (("normal",5,"a"),("burst",8,"b"))], "details": {"private": "never expose"}}
        self.write()
    def write(self): (self.root/"status.json").write_text(json.dumps(self.value))
    def test_read_only_route_exposes_only_public_profile_metadata(self):
        before = (self.root/"status.json").read_bytes()
        with TestClient(create_app(remediation_dir=self.tmp.name)) as client:
            response = client.get("/api/v1/php-fpm/profiles")
            self.assertEqual(response.status_code, 200); self.assertEqual(response.headers["cache-control"], "no-store")
            data = response.json(); self.assertEqual(data["recorded_profile"], "normal")
            self.assertEqual([p["max_children"] for p in data["profiles"]], [5,8])
            self.assertNotIn("never expose", response.text); self.assertNotIn("content", response.text)
            self.assertEqual(client.post("/api/v1/php-fpm/profiles",json={}).status_code,405)
        self.assertEqual((self.root/"status.json").read_bytes(), before)
    def test_unknown_missing_future_or_malformed_record_does_not_invent_a_profile(self):
        for change in ("future", "limit", "duplicate"):
            if change == "future": self.value["recorded_at"] = time.time()+100
            if change == "limit": self.value["profiles"][0]["max_children"] = 50
            if change == "duplicate": self.value["profiles"][1]["id"] = "normal"
            self.write(); self.assertEqual(read_status(self.tmp.name,time.time())["status"], "unavailable")
        (self.root/"status.json").unlink()
        self.assertEqual(read_status(self.tmp.name,time.time())["reason"], "profiles_not_prepared")
    def test_incomplete_old_transaction_requires_operator_review(self):
        (self.root/"pending.json").write_text("{}")
        self.assertTrue(read_status(self.tmp.name,time.time())["review_required"])
        self.value.update(state="changing",recorded_at=time.time()-1); self.write()
        self.assertEqual(read_status(self.tmp.name,time.time())["state"], "changing")
        self.value["recorded_at"] -= 100; self.write()
        self.assertTrue(read_status(self.tmp.name,time.time())["review_required"])


class QueueDecisionTests(unittest.TestCase):
    def test_cumulative_counter_does_not_trigger_and_unknown_collection_does_not_recover(self):
        def state(d): return next(o.state for o in observations(snapshot(),None,model_enabled=False,applications=d) if o.key=="php_pool_queue")
        self.assertEqual(state(application(queue=0,idle=1)),"clear")
        self.assertEqual(state(application()),"triggered")
        self.assertEqual(state(application(age=31)),"unknown")
        self.assertEqual(state(application(queue=None)),"unknown")
    def test_queue_requires_two_confirmations_and_healthy_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = IncidentStore(tmp); store.initialize(); now = int(time.time())//30*30-60
            for point in (now,now+30):
                store.apply(point,observations(snapshot(),None,model_enabled=False,applications=application()),completed_at=point+1)
            result = summarize(store,now+32)
            d = next(d for d in result["decisions"] if d["signal_key"]=="php_pool_queue")
            self.assertEqual(d["status"],"recommendation"); self.assertEqual(d["recommended_action"]["id"],"diagnose_php_pool")
            self.assertFalse(d["execution_allowed"])
            self.assertEqual(result["collector"]["processed_signals"],len(DEFINITIONS))
            bad = snapshot(); bad["collection"]["linux"]["status"] = "down"
            store.apply(now+60,observations(bad,None,model_enabled=False,applications=application()),completed_at=now+61)
            d = next(d for d in summarize(store,now+62)["decisions"] if d["signal_key"]=="php_pool_queue")
            self.assertEqual(d["status"],"blocked")
