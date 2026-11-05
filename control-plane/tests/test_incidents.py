from contextlib import asynccontextmanager
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

from app.incidents import DEFINITIONS, IncidentStore, IncidentStorageError, Observation
from app.main import create_app

BASE = 1_700_000_010


def batch(state="clear", key="cpu_high", context="policy-v1"):
    return [Observation(name,state if name==key else "clear",{"test_state":state},context if name==key else "policy-v1")
            for name in DEFINITIONS]


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store=IncidentStore(self.directory.name)
        self.store.initialize()
        self.index=0

    def apply(self,state="clear",key="cpu_high",context="policy-v1",point=None):
        point=BASE+self.index*30 if point is None else point
        self.index+=1
        return self.store.apply(point,batch(state,key,context),completed_at=point+1)

    def opened(self):
        self.apply("triggered")
        self.apply("triggered")
        return self.store.listing()["items"][0]

    def kinds(self,identifier=1):
        return [e["kind"] for e in self.store.events(identifier)["items"]]

    def test_two_checks_open_one_episode_and_repeated_alerts_do_not_duplicate_it(self):
        self.apply("triggered")
        self.assertEqual(self.store.listing()["total"],0)
        for _ in range(12):
            self.apply("triggered")
        listing=self.store.listing()
        self.assertEqual(listing["total"],1)
        self.assertEqual(listing["items"][0]["trigger_observations"],13)
        self.assertEqual(self.kinds(),["opened"])

    def test_three_recovery_checks_are_required_and_alert_interrupts_recovery(self):
        self.opened()
        self.apply("clear")
        self.apply("clear")
        self.assertEqual(self.store.detail(1)["status"],"open")
        self.apply("triggered")
        self.apply("clear")
        self.apply("clear")
        self.assertEqual(self.store.detail(1)["status"],"open")
        self.apply("clear")
        self.assertEqual(self.store.detail(1)["status"],"resolved")
        self.assertEqual(self.kinds(),["opened","resolved"])

    def test_unknown_and_hysteresis_hold_never_resolve_an_episode(self):
        self.opened()
        self.apply("clear")
        self.apply("unknown")
        self.apply("unknown")
        self.apply("hold")
        self.apply("clear")
        self.apply("clear")
        self.assertEqual(self.store.detail(1)["status"],"open")
        self.assertEqual(self.kinds(),["opened","observation_unavailable","observation_resumed"])
        self.apply("clear")
        self.assertEqual(self.store.detail(1)["status"],"resolved")

    def test_unknown_gap_and_context_change_reset_confirmation(self):
        for disruption in ("unknown","gap","context"):
            with tempfile.TemporaryDirectory() as root:
                store=IncidentStore(root)
                store.initialize()
                store.apply(BASE,batch("triggered"),completed_at=BASE)
                if disruption=="unknown":
                    store.apply(BASE+30,batch("unknown"),completed_at=BASE+30)
                point=BASE+150 if disruption=="gap" else BASE+60
                context="new-model" if disruption=="context" else "policy-v1"
                store.apply(point,batch("triggered",context=context),completed_at=point)
                self.assertEqual(store.listing()["total"],0,disruption)
                store.apply(point+30,batch("triggered",context=context),completed_at=point+30)
                self.assertEqual(store.listing()["total"],1,disruption)

    def test_restart_preserves_pending_checks_open_episode_and_events(self):
        self.apply("triggered")
        self.store=IncidentStore(self.directory.name)
        self.store.initialize()
        self.apply("triggered")
        self.apply("unknown")
        before=self.store.listing()
        self.store=IncidentStore(self.directory.name)
        self.store.initialize()
        self.assertEqual(self.store.listing(),before)
        self.apply("triggered")
        self.assertEqual(self.store.listing()["total"],1)
        self.assertEqual(self.kinds(),["opened","observation_unavailable","observation_resumed"])

    def test_new_failure_after_resolution_creates_a_distinct_episode(self):
        self.opened()
        for _ in range(3):
            self.apply("clear")
        self.apply("triggered")
        self.apply("triggered")
        self.assertEqual(self.store.listing(status="open")["total"],1)
        self.assertEqual(self.store.listing(status="resolved")["total"],1)
        self.assertEqual(self.store.listing()["items"][0]["id"],2)

    def test_duplicate_or_old_points_cannot_increase_checks_or_create_events(self):
        self.apply("triggered")
        self.assertEqual(self.apply("triggered",point=BASE),0)
        self.assertEqual(self.apply("triggered",point=BASE-30),0)
        self.assertEqual(self.store.listing()["total"],0)
        self.apply("triggered",point=BASE+30)
        self.assertEqual(self.kinds(),["opened"])
        self.assertEqual(self.store.detail(1)["trigger_observations"],2)
        self.apply("triggered",point=BASE)
        summary=self.store.summary(BASE+1)
        self.assertEqual(summary["collector"]["status"],"clock_inconsistent")

    def test_large_gap_does_not_count_as_contiguous_recovery(self):
        self.opened()
        self.apply("clear")
        self.apply("clear")
        self.apply("clear",point=BASE+300)
        self.assertEqual(self.store.detail(1)["status"],"open")
        self.assertIn("observation_gap",self.kinds())
        self.apply("clear",point=BASE+330)
        self.apply("clear",point=BASE+360)
        self.assertEqual(self.store.detail(1)["status"],"resolved")

    def test_context_changes_are_recorded_without_duplicating_active_incident(self):
        self.opened()
        self.apply("triggered",context="new-model")
        self.assertEqual(self.store.listing()["total"],1)
        self.assertEqual(self.kinds(),["opened","context_changed"])

    def test_mid_transaction_failure_rolls_back_signals_incidents_and_collector(self):
        self.apply("triggered")
        previous=self.store.summary(BASE+2)
        original=self.store.apply_one
        calls=[]
        def fail(connection,point,observation):
            if calls:
                raise RuntimeError("simulated interrupted transaction")
            calls.append(observation.key)
            return original(connection,point,observation)
        with patch.object(self.store,"apply_one",side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.apply("triggered")
        self.assertEqual(self.store.summary(BASE+2),previous)
        self.assertEqual(self.store.listing()["total"],0)

    def test_invalid_observations_do_not_modify_storage(self):
        for value in ([None],batch()+batch(),[Observation(k,"bad",{}) for k in DEFINITIONS],
                      [Observation(k,"triggered",{"x":float("nan")}) for k in DEFINITIONS]):
            with self.assertRaises(ValueError):
                self.store.apply(BASE,value,completed_at=BASE)
        self.assertEqual(self.store.listing()["total"],0)

    def test_incompatible_database_is_not_replaced_and_reader_cannot_write(self):
        with tempfile.TemporaryDirectory() as directory:
            store=IncidentStore(directory)
            with sqlite3.connect(store.path) as connection:
                connection.execute("CREATE TABLE existing_work(value TEXT)")
                connection.execute("INSERT INTO existing_work VALUES('preserved')")
            with self.assertRaises(IncidentStorageError):
                store.initialize()
            with sqlite3.connect(store.path) as connection:
                self.assertEqual(connection.execute("SELECT value FROM existing_work").fetchone()[0],"preserved")
        self.opened()
        with self.assertRaises(IncidentStorageError):
            with self.store.connection() as connection:
                connection.execute("DELETE FROM incidents")
        self.assertEqual(self.store.listing()["total"],1)

    def test_uncommitted_process_crash_is_recovered_by_the_writer(self):
        self.opened()
        before=self.store.listing()
        code="""import os,sqlite3,sys
connection=sqlite3.connect(sys.argv[1],isolation_level=None)
connection.execute('PRAGMA cache_size=1')
connection.execute('BEGIN IMMEDIATE')
connection.execute('UPDATE incidents SET trigger_observations=999')
connection.execute('UPDATE metadata SET value=?',('x'*300000,))
os._exit(23)
"""
        result=subprocess.run([sys.executable,"-c",code,str(self.store.path)],capture_output=True,timeout=5)
        self.assertEqual(result.returncode,23)
        self.store.initialize()
        self.assertEqual(self.store.listing(),before)

    def test_stale_collector_is_explicit_and_pagination_is_bounded(self):
        self.opened()
        self.assertEqual(self.store.summary(BASE+35)["collector"]["status"],"running")
        self.assertEqual(self.store.summary(BASE+200)["collector"]["status"],"stale")
        with self.assertRaises(ValueError):
            self.store.listing(limit=101)
        self.assertEqual(self.store.events(999),None)
        self.assertEqual(len(self.store.events(1,limit=1)["items"]),1)

    def test_delayed_completion_does_not_make_an_old_observation_current(self):
        self.store.apply(BASE,batch(),completed_at=BASE+300)
        collector=self.store.summary(BASE+301)["collector"]
        self.assertEqual(collector["age_seconds"],1)
        self.assertEqual(collector["input_age_seconds"],301)
        self.assertEqual(collector["status"],"stale")


class IncidentAPITests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def client(self,*,enabled=True,initialize=True):
        with tempfile.TemporaryDirectory() as directory:
            store=IncidentStore(directory)
            if initialize:
                store.initialize()
                for i in range(2):
                    store.apply(BASE+i*30,batch("triggered"),completed_at=BASE+i*30)
            application=create_app(incidents_dir=directory if enabled else "",anomalies_dir="")
            async with application.router.lifespan_context(application):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),base_url="http://testserver") as client:
                    yield client

    async def test_list_detail_timeline_and_summary_read_persisted_records(self):
        async with self.client() as client:
            for path in ("/api/v1/incidents","/api/v1/incidents/1","/api/v1/incidents/1/events","/api/v1/incidents/summary"):
                response=await client.get(path)
                self.assertEqual(response.status_code,200,path)
            self.assertEqual((await client.get("/api/v1/incidents/summary")).json()["counts"]["open"],1)
            self.assertEqual((await client.get("/api/v1/incidents/1/events")).json()["items"][0]["kind"],"opened")

    async def test_disabled_or_missing_storage_returns_503_without_breaking_health(self):
        for kwargs in ({"enabled":False},{"initialize":False}):
            async with self.client(**kwargs) as client:
                self.assertEqual((await client.get("/api/v1/incidents")).status_code,503)
                self.assertEqual((await client.get("/api/v1/health")).status_code,200)

    async def test_unknown_incidents_return_404_and_query_parameters_are_validated(self):
        async with self.client() as client:
            for path in ("/api/v1/incidents/999","/api/v1/incidents/999/events"):
                self.assertEqual((await client.get(path)).status_code,404)
            for query in ("limit=101","offset=-1","status=deleted"):
                self.assertEqual((await client.get("/api/v1/incidents?"+query)).status_code,422)

    async def test_http_reads_do_not_insert_update_or_resolve_an_incident(self):
        async with self.client() as client:
            before=(await client.get("/api/v1/incidents")).json()
            for _ in range(3):
                await client.get("/api/v1/incidents/summary")
            after=(await client.get("/api/v1/incidents")).json()
            self.assertEqual(before,after)
            self.assertEqual((await client.post("/api/v1/incidents/1")).status_code,405)


if __name__=="__main__":
    unittest.main()
