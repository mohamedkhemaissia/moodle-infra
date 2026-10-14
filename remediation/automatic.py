"""Worker automatique : au plus une tentative par incident et revision."""
import argparse
import json
import os
import sys
import time

from diagnostics.collector import get_json, ProbeError
from diagnostics.store import DiagnosticStorageError
from pilotage.operating_policy import PolicyStore, PolicyStorageError, PolicyBlocked
from .action import config_read
from .run import CONFIG, execute


def once(config, config_path, *, store=None, reader=get_json, executor=execute):
    store=store or PolicyStore()
    try:
        view=store.view(); policy=view["policy"]
        if not config.get("policy_required",False): raise PolicyBlocked("automatic_requires_operating_policy")
        if policy["mode"] != "automatic" or policy["suspended"]:
            store.heartbeat("idle")
            return {"status":"idle","reason":"automatic_not_active"},0
        summary=reader(config["api_url"]+"/api/v1/decisions/summary")
        if summary.get("status") != "available" or summary.get("snapshot_usable") is not True or not isinstance(summary.get("decisions"),list):
            raise PolicyBlocked("observation_unusable")
        for decision in summary["decisions"]:
            if decision.get("signal_key") not in ("moodle_https", "php_pool_queue") or decision.get("status") != "recommendation": continue
            if decision.get("signal_key") == "php_pool_queue" and not config.get("php_profiles_enabled", False): continue
            document={**summary,"decision":decision}; document.pop("decisions")
            selected=store.reserve_worker(document)
            if selected is None: continue
            store.heartbeat("running")
            result,code=executor(config,config_path,decision["incident_id"],automatic=True,expected_policy=selected)
            store.heartbeat("idle",run_id=result.get("run_id"),reason=result.get("status"))
            return result,0 if result.get("status") in ("completed","blocked","busy") else code
        store.heartbeat("idle")
        return {"status":"idle","reason":"no_new_eligible_incident"},0
    except (PolicyBlocked,ProbeError) as exc:
        store.heartbeat("idle",reason=str(exc))
        return {"status":"blocked","reason":str(exc)[:120]},0
    except (PolicyStorageError,DiagnosticStorageError,OSError,ValueError,KeyError,TypeError):
        return {"status":"unavailable","reason":"policy_or_observation_unavailable"},1


def healthcheck(config, *, store=None):
    """Verifier la configuration et PostgreSQL sans reserver ni executer d'action."""
    try:
        if config.get("policy_required") is not True:
            raise PolicyBlocked("automatic_requires_operating_policy")
        view=(store or PolicyStore()).view()
        policy=view["policy"]
        if policy["mode"] not in ("observation","assisted","automatic") or type(policy["suspended"]) is not bool:
            raise ValueError("invalid_policy")
        return {"status":"available","mode":policy["mode"],"suspended":policy["suspended"],
                "action_scope":["start_php_stopped"]},0
    except (PolicyBlocked,PolicyStorageError,OSError,ValueError,KeyError,TypeError):
        return {"status":"unavailable","reason":"configuration_or_policy_unavailable"},1


def run_loop(config, config_path, interval, *, passage=once, pause=time.sleep):
    while True:
        result,_=passage(config,config_path)
        print(json.dumps(result,ensure_ascii=False),flush=True)
        pause(interval)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument("--loop",action="store_true",help="Executer un passage periodique.")
    mode.add_argument("--healthcheck",action="store_true",help="Verifier sans reserver d'action.")
    parser.add_argument("--interval-seconds",type=float,default=30.0)
    args=parser.parse_args(argv)
    if not 5 <= args.interval_seconds <= 300:
        parser.error("L'intervalle doit etre compris entre 5 et 300 secondes.")
    os.umask(0o027)
    try:
        config=config_read(CONFIG)
        if args.healthcheck:
            result,code=healthcheck(config)
        elif args.loop:
            run_loop(config,CONFIG,args.interval_seconds)
            return 0
        else:
            result,code=once(config,CONFIG)
    except (OSError,ValueError,PolicyStorageError):
        result,code={"status":"unavailable","reason":"configuration_or_policy_unavailable"},1
    print(json.dumps(result,ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
