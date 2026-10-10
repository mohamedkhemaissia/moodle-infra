import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import time
import unittest
from unittest.mock import AsyncMock, patch

from app.incident_observer import poll, writer_lock
from app.incident_signals import observations
from app.incidents import IncidentStore, IncidentStorageError, DEFINITIONS
from app.prometheus import PrometheusUnavailable
import test_anomalies as model_fixtures

BASE=1_700_000_010


def snapshot(cpu=10,memory=30,disk=40,available=True,linux="up",moodle="up"):
    return {"collection":{"linux":{"status":linux,"scrape_age_seconds":1},
                          "moodle_https":{"status":moodle,"scrape_age_seconds":1}},
            "metrics":{"cpu_usage_percent":cpu,"memory_usage_percent":memory,"disk_available_percent":disk,
                       "moodle_available":available,"moodle_http_status_code":200 if available else 0,
                       "moodle_probe_duration_ms":60}}


def analysis(score=.4,threshold=.6,reason=None,point=BASE):
    data={"status":"unavailable" if reason else "available","model_id":"a"*64,
          "input_evaluated_at":datetime.fromtimestamp(point,timezone.utc).isoformat().replace("+00:00","Z")}
    if reason:
        data.update(reason=reason,required_history_seconds=420,invalid_points=[{}])
    else:
        data["anomaly"]={"score":score,"threshold":threshold}
    return data


class SignalTests(unittest.TestCase):
    def get(self,data,ml=None,**kwargs):
        return {o.key:o for o in observations(data,ml or analysis(),**kwargs)}

    def test_resource_thresholds_hysteresis_and_low_disk_direction(self):
        flags=self.get(snapshot(cpu=85,memory=90,disk=10))
        for key in ("cpu_high","memory_high","disk_low"):
            self.assertEqual(flags[key].state,"triggered")
        flags=self.get(snapshot(cpu=80,memory=88,disk=12))
        for key in ("cpu_high","memory_high","disk_low"):
            self.assertEqual(flags[key].state,"hold")
        flags=self.get(snapshot(cpu=0,memory=85,disk=15))
        for key in ("cpu_high","memory_high","disk_low"):
            self.assertEqual(flags[key].state,"clear")

    def test_missing_or_unhealthy_collection_cannot_create_a_resource_or_moodle_failure(self):
        flags=self.get(snapshot(cpu=99,available=False,linux="stale",moodle="down"))
        self.assertEqual(flags["cpu_high"].state,"unknown")
        self.assertEqual(flags["moodle_https"].state,"unknown")
        self.assertEqual(flags["linux_collection"].state,"triggered")
        self.assertEqual(flags["moodle_collection"].state,"triggered")
        self.assertEqual(self.get(snapshot(available=False))["moodle_https"].state,"triggered")

    def test_incomplete_non_finite_and_out_of_domain_metrics_are_data_quality_problems(self):
        for value in (None,float("nan"),float("inf"),101,-1,True):
            flags=self.get(snapshot(cpu=value))
            self.assertEqual(flags["cpu_high"].state,"unknown")
            self.assertEqual(flags["linux_collection"].state,"triggered")
            json.dumps([o.evidence for o in flags.values()],allow_nan=False)
        flags=self.get(snapshot(available=None))
        self.assertEqual(flags["moodle_https"].state,"unknown")
        self.assertEqual(flags["moodle_collection"].state,"triggered")

    def test_statistical_alert_is_independent_and_insufficient_history_is_unknown(self):
        flags=self.get(snapshot(),analysis(score=.7))
        self.assertEqual(flags["statistical_anomaly"].state,"triggered")
        self.assertEqual(flags["cpu_high"].state,"clear")
        self.assertEqual(self.get(snapshot(),analysis(score=.59))["statistical_anomaly"].state,"hold")
        flags=self.get(snapshot(),analysis(reason="insufficient_contiguous_history"))
        self.assertEqual(flags["analysis_history"].state,"triggered")
        self.assertEqual(flags["statistical_anomaly"].state,"unknown")

    def test_single_writer_lock_is_exclusive_and_released_after_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            with writer_lock(directory):
                with self.assertRaises(IncidentStorageError):
                    with writer_lock(directory):
                        pass
            with writer_lock(directory):
                pass


class PollTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store=IncidentStore(self.directory.name)
        self.store.initialize()
        self.prometheus=AsyncMock()
        self.anomalies=AsyncMock()
        self.index=0

    async def step(self,metrics=None,ml=None,error=None):
        point=BASE+self.index*30
        self.index+=1
        self.prometheus.summary.side_effect=error
        self.prometheus.summary.return_value=metrics or snapshot()
        self.anomalies.summary.return_value=ml or analysis(point=point)
        with patch("app.incident_observer.time.time",return_value=point+2):
            return await poll(self.prometheus,self.anomalies,self.store,
                              datetime.fromtimestamp(point,timezone.utc))

    async def test_real_poll_to_database_opens_resource_episode_and_deduplicates(self):
        for _ in range(4):
            await self.step(snapshot(cpu=90))
        rows=self.store.listing()["items"]
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["signal_key"],"cpu_high")
        self.assertEqual(rows[0]["trigger_observations"],4)
        self.assertEqual(self.store.summary(BASE+100)["collector"]["status"],"running")

    async def test_prometheus_failure_preserves_existing_resource_episode(self):
        await self.step(snapshot(cpu=90))
        await self.step(snapshot(cpu=90))
        for _ in range(2):
            await self.step(error=PrometheusUnavailable("simulated transport failure"))
        self.assertEqual(self.store.detail(1)["status"],"open")
        self.assertEqual(self.store.detail(1)["latest_state"],"unknown")
        keys={row["signal_key"] for row in self.store.listing(status="open")["items"]}
        self.assertEqual(keys,{"cpu_high","prometheus_unavailable"})

    async def test_history_failure_is_separate_and_cannot_resolve_statistical_episode(self):
        await self.step(ml=analysis(score=.7))
        await self.step(ml=analysis(score=.7))
        for _ in range(2):
            await self.step(ml=analysis(reason="insufficient_contiguous_history"))
        rows=self.store.listing(status="open")["items"]
        self.assertEqual({row["signal_key"] for row in rows},{"statistical_anomaly","analysis_history"})
        for _ in range(3):
            await self.step()
        self.assertEqual(self.store.listing(status="open")["total"],0)
        self.assertEqual(self.store.listing(status="resolved")["total"],2)

    async def test_missing_model_records_quality_problem_and_keeps_metrics_observation(self):
        self.prometheus.summary.return_value=snapshot(cpu=90)
        for index in range(2):
            point=BASE+index*30
            with patch("app.incident_observer.time.time",return_value=point+2):
                await poll(self.prometheus,None,self.store,datetime.fromtimestamp(point,timezone.utc),model_enabled=True)
        keys={row["signal_key"] for row in self.store.listing()["items"]}
        self.assertEqual(keys,{"cpu_high","anomaly_model"})

    async def test_clock_rollback_during_poll_does_not_commit_false_confirmation(self):
        self.prometheus.summary.return_value=snapshot(cpu=90)
        self.anomalies.summary.return_value=analysis()
        with patch("app.incident_observer.time.time",return_value=BASE-1):
            with self.assertRaises(IncidentStorageError):
                await poll(self.prometheus,self.anomalies,self.store,datetime.fromtimestamp(BASE,timezone.utc))
        self.assertEqual(self.store.listing()["total"],0)
        self.assertIsNone(self.store.summary(BASE)["collector"])


class ObserverCLITests(unittest.TestCase):
    def test_healthcheck_is_read_only_and_does_not_import_collection_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            store=IncidentStore(directory)
            store.initialize()
            completed=time.time()
            point=int(completed)//30*30
            store.apply(point,observations(snapshot(),analysis(point=point)),completed_at=completed)
            database=Path(directory)/"incidents.sqlite3"
            before=database.read_bytes()
            names=set(Path(directory).iterdir())
            environment={**os.environ,"INCIDENTS_DIR":directory,
                         "ANOMALIES_DIR":"/modele-inaccessible-pour-un-healthcheck"}
            script="""
import importlib.abc
import runpy
import sys
class RejectCollectionDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'httpx','pydantic','fastapi','numpy','scipy','sklearn'} or fullname in {
            'app.anomalies','app.prometheus','app.resource_history','app.models','app.isolation_model'}:
            raise RuntimeError('Dependance de collecte importee par le healthcheck: '+fullname)
sys.meta_path.insert(0, RejectCollectionDependencies())
sys.argv=['app.incident_observer','--healthcheck']
runpy.run_module('app.incident_observer',run_name='__main__')
"""
            result=subprocess.run([sys.executable,"-B","-c",script],env=environment,
                                  capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout,"")
            self.assertEqual(database.read_bytes(),before)
            self.assertEqual(set(Path(directory).iterdir()),names)

    def test_healthcheck_still_rejects_absent_uninitialized_or_stale_observations(self):
        for state in ("absent","uninitialized","stale_input","stale_completed"):
            with self.subTest(state=state),tempfile.TemporaryDirectory() as directory:
                store=IncidentStore(directory)
                if state != "absent":
                    store.initialize()
                if state.startswith("stale"):
                    now=time.time()
                    point=int(now-150)//30*30
                    completed=now if state == "stale_input" else now-150
                    store.apply(point,observations(snapshot(),analysis(point=point)),completed_at=completed)
                before={file.name:file.read_bytes() for file in Path(directory).iterdir()}
                environment={**os.environ,"INCIDENTS_DIR":directory}
                result=subprocess.run([sys.executable,"-B","-m","app.incident_observer","--healthcheck"],
                                      env=environment,capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,1,result.stderr)
                self.assertIn("Observateur indisponible",result.stderr)
                self.assertEqual({file.name:file.read_bytes() for file in Path(directory).iterdir()},before)

    def test_process_initializes_persistent_storage_and_health_reads_it(self):
        with tempfile.TemporaryDirectory() as directory:
            environment={**os.environ,"INCIDENTS_DIR":directory,"ANOMALIES_DIR":"",
                         "PROMETHEUS_URL":"http://127.0.0.1:1","MAX_SCRAPE_AGE_SECONDS":"60"}
            result=subprocess.run([sys.executable,"-B","-m","app.incident_observer","--once"],
                                  env=environment,capture_output=True,text=True,timeout=12)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)["processed_signals"],len(DEFINITIONS))
            self.assertTrue((Path(directory)/"incidents.sqlite3").exists())
            health=subprocess.run([sys.executable,"-B","-m","app.incident_observer","--healthcheck"],
                                  env=environment,capture_output=True,text=True,timeout=30)
            self.assertEqual(health.returncode,0,health.stderr)
            self.assertEqual(IncidentStore(directory).listing()["total"],0)

    def test_incompatible_model_grid_is_not_used_by_the_observer(self):
        with tempfile.TemporaryDirectory() as directory,tempfile.TemporaryDirectory() as models:
            model,report,metadata=model_fixtures.fixture()
            metadata["step_seconds"]=15
            metadata["window_points_including_endpoints"]=21
            metadata["source_range"]["step_seconds"]=15
            model_fixtures.write_bundle(models,model,report,metadata)
            environment={**os.environ,"INCIDENTS_DIR":directory,"ANOMALIES_DIR":models,
                         "PROMETHEUS_URL":"http://127.0.0.1:1","MAX_SCRAPE_AGE_SECONDS":"60"}
            result=subprocess.run([sys.executable,"-B","-m","app.incident_observer","--once"],
                                  env=environment,capture_output=True,text=True,timeout=12)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)["anomaly_error"],"model_unavailable")


if __name__=="__main__":
    unittest.main()
