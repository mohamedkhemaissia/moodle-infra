import copy
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from app.decisions import RULES, evaluate, policy_document, summarize
from app.incident_signals import observations
from app.incidents import DEFINITIONS, IncidentStore, IncidentStorageError, Observation
from app.main import create_app

BASE=1_700_000_010
MODEL="e"*64


def metrics(cpu=10,memory=30,disk=40,available=True,linux="up",moodle="up"):
    return {"collection":{"linux":{"status":linux,"scrape_age_seconds":1},
                          "moodle_https":{"status":moodle,"scrape_age_seconds":1}},
            "metrics":{"cpu_usage_percent":cpu,"memory_usage_percent":memory,"disk_available_percent":disk,
                       "moodle_available":available,"moodle_http_status_code":200 if available else 503,
                       "moodle_probe_duration_ms":60}}


def analysis(point,score=.4,history=True):
    result={"status":"available" if history else "unavailable","model_id":MODEL,
            "input_evaluated_at":datetime.fromtimestamp(point,timezone.utc).isoformat().replace("+00:00","Z")}
    if history:
        result["anomaly"]={"score":score,"threshold":.646}
    else:
        result.update(reason="insufficient_contiguous_history",required_history_seconds=420,invalid_points=[{}]*12)
    return result


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store=IncidentStore(self.directory.name)
        self.store.initialize()
        self.index=0

    def step(self,data=None,*,score=.4,history=True,extra=None,point=None):
        point=BASE+self.index*30 if point is None else point
        self.index+=1
        signals=observations(data or metrics(),analysis(point,score,history))
        if extra:
            signals=[extra.get(item.key,item) for item in signals]
        self.store.apply(point,signals,completed_at=point+1)
        return point+2

    def open_cpu(self):
        self.step(metrics(cpu=90))
        return self.step(metrics(cpu=90))

    def one(self,now,key="cpu_high"):
        return next(item for item in summarize(self.store,now)["decisions"] if item["signal_key"]==key)

    def test_history_quality_incident_proposes_contiguous_collection(self):
        self.step(history=False)
        now=self.step(history=False)
        decision=self.one(now,"analysis_history")
        self.assertEqual(decision["status"],"recommendation")
        self.assertEqual(decision["recommended_action"]["id"],"wait_contiguous_history")
        self.assertEqual(decision["recommended_action"]["required_history_seconds"],420)
        self.assertEqual(decision["evidence"]["invalid_point_count"],12)
        self.assertFalse(decision["execution_allowed"])

    def test_confirmed_resource_and_https_alerts_propose_diagnostics_only(self):
        self.step(metrics(cpu=90,memory=95,disk=8,available=False))
        now=self.step(metrics(cpu=90,memory=95,disk=8,available=False))
        result=summarize(self.store,now)
        self.assertEqual({item["recommended_action"]["id"] for item in result["decisions"]},
                         {"diagnose_cpu_usage","diagnose_memory_usage","diagnose_disk_usage","diagnose_moodle_https"})
        self.assertEqual(result["decisions"][0]["signal_key"],"moodle_https")
        self.assertFalse(result["execution"]["enabled"])
        for item in result["decisions"]:
            self.assertFalse(item["recommended_action"]["executable"])
            self.assertEqual(len(item["related_incidents"]),3)

    def test_stale_delayed_and_reversed_clock_inputs_block_recommendations(self):
        now=self.open_cpu()
        stale=self.one(now+120)
        self.assertEqual(stale["status"],"blocked")
        self.assertIn("observer_stale",stale["reason_codes"])
        future=self.one(BASE)
        self.assertIn("clock_inconsistent",future["reason_codes"])
        point=BASE+30
        self.store.apply(point,observations(metrics(cpu=90),analysis(point)),completed_at=point+200)
        delayed=self.one(point+201)
        self.assertIn("observer_stale",delayed["reason_codes"])
        self.assertIsNone(delayed["recommended_action"])

    def test_recovery_hold_and_unknown_states_preserve_episode_without_recommendation(self):
        self.open_cpu()
        now=self.step(metrics(cpu=10))
        self.assertEqual(self.one(now)["status"],"monitoring")
        now=self.step(metrics(cpu=80))
        self.assertIn("hysteresis_hold",self.one(now)["reason_codes"])
        now=self.step(metrics(cpu=99,linux="stale"),history=False)
        decision=self.one(now)
        self.assertEqual(decision["status"],"blocked")
        self.assertIn("current_signal_unknown",decision["reason_codes"])
        self.assertEqual(self.store.detail(1)["status"],"open")

    def test_gap_and_context_changes_require_reconfirmation(self):
        self.open_cpu()
        now=self.step(metrics(cpu=90),point=BASE+180)
        self.assertIn("trigger_reconfirmation_required",self.one(now)["reason_codes"])
        now=self.step(metrics(cpu=90),point=BASE+210)
        self.assertEqual(self.one(now)["status"],"recommendation")
        changed=Observation("cpu_high","triggered",self.store.detail(1)["evidence"],"policy-v2")
        now=self.step(metrics(cpu=90),extra={"cpu_high":changed},point=BASE+240)
        self.assertIn("trigger_reconfirmation_required",self.one(now)["reason_codes"])

    def test_dependency_quality_is_checked_even_if_resource_signal_claims_triggered(self):
        now=self.open_cpu()
        signals=observations(metrics(cpu=90),analysis(BASE+60))
        signals=[Observation("linux_collection","triggered",{"collection_status":"stale"}) if item.key=="linux_collection" else item for item in signals]
        self.store.apply(BASE+60,signals,completed_at=BASE+61)
        decision=self.one(BASE+62)
        self.assertIn("dependency_unhealthy:linux_collection",decision["reason_codes"])
        self.assertIsNone(decision["recommended_action"])

    def test_statistical_recommendation_is_experimental_and_never_a_restart(self):
        self.step(score=.8)
        now=self.step(score=.8)
        decision=self.one(now,"statistical_anomaly")
        self.assertEqual(decision["recommended_action"]["id"],"investigate_statistical_anomaly")
        self.assertTrue(decision["evidence"]["experimental"])
        self.assertFalse(decision["execution_allowed"])
        snapshot=self.store.decision_snapshot(now)
        snapshot["signals"]["analysis_history"]["context"]="model:"+"a"*64
        decision=evaluate(snapshot,now)["decisions"][0]
        self.assertIn("analysis_model_context_inconsistent",decision["reason_codes"])

    def test_invalid_trigger_evidence_and_unknown_policy_fail_closed(self):
        now=self.open_cpu()
        snapshot=self.store.decision_snapshot(now)
        for evidence in ({"value":90},{**snapshot["incidents"][0]["evidence"],"value":10}):
            altered=copy.deepcopy(snapshot)
            altered["incidents"][0]["evidence"]=evidence
            altered["signals"]["cpu_high"]["evidence"]=evidence
            decision=evaluate(altered,now)["decisions"][0]
            self.assertIn("trigger_evidence_inconsistent",decision["reason_codes"])
        altered=copy.deepcopy(snapshot)
        altered["incidents"][0]["signal_key"]="action-inconnue"
        decision=evaluate(altered,now)["decisions"][0]
        self.assertIn("no_policy_for_signal",decision["reason_codes"])
        self.assertIsNone(decision["recommended_action"])

    def test_decision_and_validity_are_stable_for_same_observation(self):
        now=self.open_cpu()
        first=self.one(now)
        second=self.one(now+5)
        self.assertEqual(first["decision_id"],second["decision_id"])
        self.assertEqual(first["valid_until"],second["valid_until"])
        self.assertEqual(first["valid_until"],datetime.fromtimestamp(BASE+120,timezone.utc).isoformat().replace("+00:00","Z"))
        self.assertEqual(len(first["policy_sha256"]),64)
        now=self.step(metrics(cpu=90))
        self.assertNotEqual(first["decision_id"],self.one(now)["decision_id"])

    def test_incomplete_mixed_or_conflicting_snapshot_blocks(self):
        now=self.open_cpu()
        original=self.store.decision_snapshot(now)
        for kind in ("missing","mixed","conflict","incomplete_collector"):
            with self.subTest(kind=kind):
                snapshot=copy.deepcopy(original)
                if kind=="missing":
                    del snapshot["signals"]["memory_high"]
                elif kind=="mixed":
                    snapshot["signals"]["memory_high"]["last_observed_at"]=datetime.fromtimestamp(BASE,timezone.utc).isoformat().replace("+00:00","Z")
                elif kind=="conflict":
                    snapshot["signals"]["cpu_high"]["context"]="different-context"
                else:
                    snapshot["collector"]["processed_signals"]=0
                result=evaluate(snapshot,now)
                self.assertEqual(result["decisions"][0]["status"],"blocked")

    def test_resolved_episode_has_no_action_and_summary_lists_only_open_episodes(self):
        self.open_cpu()
        for _ in range(3):
            now=self.step()
        self.assertEqual(summarize(self.store,now)["decisions"],[])
        result=summarize(self.store,now,1)
        self.assertEqual(result["decision"]["status"],"closed")
        self.assertIsNone(result["decision"]["recommended_action"])
        self.assertIsNone(summarize(self.store,now,999))

    def test_reading_decisions_preserves_database_and_survives_new_reader(self):
        now=self.open_cpu()
        before=self.store.path.read_bytes()
        events=self.store.events(1)
        for _ in range(3):
            summarize(IncidentStore(self.directory.name),now)
        self.assertEqual(self.store.path.read_bytes(),before)
        self.assertEqual(self.store.events(1),events)

    def test_bad_collector_metadata_is_rejected_without_modifying_database(self):
        self.open_cpu()
        with sqlite3.connect(self.store.path) as connection:
            connection.execute("UPDATE metadata SET value='5' WHERE key='collector'")
        before=self.store.path.read_bytes()
        with self.assertRaises(IncidentStorageError):
            summarize(self.store,BASE+32)
        self.assertEqual(self.store.path.read_bytes(),before)

    def test_snapshot_keeps_one_transaction_while_observer_commits_a_new_cycle(self):
        now=self.open_cpu()
        reached_commit=threading.Event()
        failures=[]
        original_reader=IncidentStore.read_collector
        class Writer(IncidentStore):
            @contextmanager
            def connection(self,**kwargs):
                with super().connection(**kwargs) as connection:
                    connection.set_trace_callback(lambda sql: reached_commit.set() if sql=="COMMIT" else None)
                    yield connection
        writer=Writer(self.directory.name)
        def write_cycle():
            try:
                writer.apply(BASE+60,observations(metrics(),analysis(BASE+60)),completed_at=BASE+61)
            except Exception as exc:
                failures.append(exc)
        thread=threading.Thread(target=write_cycle)
        def concurrent_read(connection,at):
            result=original_reader(connection,at)
            thread.start()
            self.assertTrue(reached_commit.wait(1))
            return result
        with patch.object(IncidentStore,"read_collector",side_effect=concurrent_read):
            snapshot=self.store.decision_snapshot(now)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures,[])
        self.assertEqual(evaluate(snapshot,now)["decisions"][0]["status"],"recommendation")
        self.assertEqual(summarize(self.store,BASE+62)["decisions"][0]["status"],"monitoring")

    def test_empty_initialized_storage_returns_no_recommendation_and_reports_observer_missing(self):
        result=summarize(self.store,BASE)
        self.assertEqual(result["decisions"],[])
        self.assertFalse(result["snapshot_usable"])
        self.assertIn("observer_missing",result["snapshot_reason_codes"])

    def test_policy_is_complete_immutable_and_non_finite_inputs_are_rejected(self):
        self.assertEqual(set(RULES),set(DEFINITIONS))
        document=policy_document()
        document["rules"]["cpu_high"]["steps"].clear()
        self.assertTrue(policy_document()["rules"]["cpu_high"]["steps"])
        now=self.open_cpu()
        snapshot=self.store.decision_snapshot(now)
        snapshot["incidents"][0]["evidence"]["value"]=float("nan")
        with self.assertRaises(IncidentStorageError):
            evaluate(snapshot,now)
        for value in (float("nan"),float("inf"),True,-1):
            with self.assertRaises(ValueError):
                evaluate({},value)


class DecisionAPITests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def client(self,*,enabled=True,storage=True,initialize=True):
        with tempfile.TemporaryDirectory() as directory:
            store=IncidentStore(directory)
            if initialize:
                store.initialize()
                point=int(time.time())//30*30-30
                for index in range(2):
                    at=point+index*30
                    store.apply(at,observations(metrics(cpu=90),analysis(at)),completed_at=at+1)
            def reject_network(request):
                raise AssertionError("Une lecture des decisions ne doit pas interroger Prometheus.")
            application=create_app(incidents_dir=directory if storage else "",forecasts_dir="",anomalies_dir="",
                                   decisions_enabled=enabled,transport=httpx.MockTransport(reject_network))
            async with application.router.lifespan_context(application):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url="http://testserver") as client:
                    yield client,store

    async def test_summary_detail_and_policies_are_read_only_and_never_execute(self):
        async with self.client() as (client,store):
            before=store.path.read_bytes()
            for path in ("/api/v1/decisions/summary","/api/v1/decisions/1","/api/v1/decisions/policies"):
                response=await client.get(path)
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.headers["cache-control"],"no-store")
            result=(await client.get("/api/v1/decisions/1")).json()
            self.assertEqual(result["decision"]["status"],"recommendation")
            self.assertFalse(result["execution"]["enabled"])
            self.assertEqual(store.path.read_bytes(),before)
            for method in ("post","put","delete"):
                self.assertEqual((await client.request(method,"/api/v1/decisions/1")).status_code,405)
            self.assertEqual((await client.post("/api/v1/decisions/1/execute")).status_code,404)

    async def test_disabled_missing_and_corrupt_storage_do_not_break_health(self):
        for kwargs in ({"enabled":False},{"storage":False},{"initialize":False}):
            async with self.client(**kwargs) as (client,store):
                self.assertEqual((await client.get("/api/v1/decisions/summary")).status_code,503)
                self.assertEqual((await client.get("/api/v1/health")).status_code,200)
        async with self.client() as (client,store):
            store.path.write_bytes(b"Un stockage incompatible a preserver")
            self.assertEqual((await client.get("/api/v1/decisions/summary")).status_code,503)
            self.assertEqual(store.path.read_bytes(),b"Un stockage incompatible a preserver")

    async def test_unknown_invalid_ids_and_configuration_are_rejected(self):
        async with self.client() as (client,store):
            self.assertEqual((await client.get("/api/v1/decisions/999")).status_code,404)
            for value in ("0","-1","abc",str(2**63)):
                self.assertEqual((await client.get("/api/v1/decisions/"+value)).status_code,422)
        with patch.dict(os.environ,{"DECISIONS_ENABLED":"execute"}):
            with self.assertRaises(ValueError):
                create_app()
        with self.assertRaises(ValueError):
            create_app(decisions_enabled="true")


if __name__=="__main__":
    unittest.main()
