"""Observateur periodique en lecture de Prometheus ; unique ecrivain des incidents."""
import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import time

from .incident_signals import observations
from .incidents import IncidentStore, IncidentStorageError, POLICY

LOG = logging.getLogger(__name__)


@contextmanager
def writer_lock(directory):
    path = Path(directory)/"observer.lock"
    with path.open("a") as stream:
        try:
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise IncidentStorageError("Un observateur ecrit deja dans ce stockage.") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def error_code(result):
    if not isinstance(result, Exception):
        return None
    from .anomalies import AnomalyBundleError
    from .prometheus import PrometheusInvalidResponse, PrometheusUnavailable

    if isinstance(result,PrometheusUnavailable):
        return "prometheus_unavailable"
    if isinstance(result,PrometheusInvalidResponse):
        return "prometheus_invalid_response"
    if isinstance(result,AnomalyBundleError):
        return "anomaly_calculation_unavailable"
    return "unexpected_observation_error" if isinstance(result,Exception) else None


async def poll(prometheus, anomalies, store, now, *, model_enabled=True):
    metrics, anomaly, anomaly_error = None,None,None
    calls = [prometheus.summary(now)]
    if anomalies is not None:
        calls.append(anomalies.summary(now))
    results = await asyncio.gather(*calls,return_exceptions=True)
    prometheus_error = error_code(results[0])
    if prometheus_error is None:
        metrics = results[0]
    if anomalies is not None:
        anomaly_error = error_code(results[1])
        if anomaly_error is None:
            anomaly = results[1]
        if anomaly_error and anomaly_error.startswith("prometheus_"):
            prometheus_error = prometheus_error or anomaly_error
    elif model_enabled:
        anomaly_error = "model_unavailable"
    signals = observations(metrics,anomaly,prometheus_error=prometheus_error,
                           anomaly_error=anomaly_error,model_enabled=model_enabled)
    point = int(now.timestamp())//POLICY["interval_seconds"]*POLICY["interval_seconds"]
    completed_at = time.time()
    if completed_at < now.timestamp():
        raise IncidentStorageError("Horloge reculee pendant l'observation ; aucune ecriture.")
    diagnostics = {"prometheus_error":prometheus_error,"anomaly_error":anomaly_error,
                   "analysis_status":anomaly.get("status") if anomaly is not None else "unavailable"}
    processed = await asyncio.to_thread(store.apply,point,signals,completed_at=completed_at,diagnostics=diagnostics)
    return {"input_evaluated_at":now.isoformat(),"processed_signals":processed,**diagnostics}


async def run(directory, *, once=False):
    # Le healthcheck lit SQLite uniquement ; ces dependances servent a la collecte.
    import httpx
    from .anomalies import AnomalyBundle, AnomalyBundleError, AnomalyService
    from .prometheus import PrometheusClient

    os.umask(0o027)
    store = IncidentStore(directory)
    with writer_lock(directory):
        store.initialize()
        model_directory = os.getenv("ANOMALIES_DIR", "")
        bundle = None
        if model_directory:
            try:
                candidate = AnomalyBundle(model_directory)
                if candidate.step != POLICY["interval_seconds"]:
                    raise AnomalyBundleError("La grille du modele doit etre de 30 secondes pour cet observateur.")
                bundle = candidate
            except AnomalyBundleError:
                LOG.error("Modele d'anomalies invalide ; observations de supervision maintenues.")
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM,signal.SIGINT):
            loop.add_signal_handler(sig,stop.set)
        try:
            async with httpx.AsyncClient(base_url=os.getenv("PROMETHEUS_URL","http://127.0.0.1:9090").rstrip("/")+"/",
                                         timeout=5,trust_env=False) as client:
                prometheus = PrometheusClient(client,float(os.getenv("MAX_SCRAPE_AGE_SECONDS","60")))
                anomalies = AnomalyService(prometheus,bundle) if bundle is not None else None
                while not stop.is_set():
                    started = loop.time()
                    result = await poll(prometheus,anomalies,store,datetime.now(timezone.utc),model_enabled=bool(model_directory))
                    print(json.dumps(result,ensure_ascii=False,allow_nan=False),flush=True)
                    if once:
                        return
                    delay = max(0.1,POLICY["interval_seconds"]-(loop.time()-started))
                    try:
                        await asyncio.wait_for(stop.wait(),timeout=delay)
                    except asyncio.TimeoutError:
                        pass
        finally:
            for sig in (signal.SIGTERM,signal.SIGINT):
                loop.remove_signal_handler(sig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once",action="store_true")
    parser.add_argument("--healthcheck",action="store_true")
    args = parser.parse_args()
    directory = os.getenv("INCIDENTS_DIR", "")
    if not directory:
        parser.exit(1,"Le repertoire persistant des incidents n'est pas configure.\n")
    try:
        if args.healthcheck:
            result = IncidentStore(directory).summary(time.time())
            collector = result["collector"]
            if collector is None or collector["status"] != "running":
                raise IncidentStorageError("Observateur des incidents absent ou obsolete.")
        else:
            asyncio.run(run(directory,once=args.once))
    except (OSError,ValueError,IncidentStorageError) as exc:
        parser.exit(1,f"Observateur indisponible : {exc}\n")


if __name__ == "__main__":
    main()
