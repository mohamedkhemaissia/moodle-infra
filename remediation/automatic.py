"""Un passage du worker : au plus une tentative par incident et revision."""
import json
import os
import sys

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
            if decision.get("signal_key") != "moodle_https" or decision.get("status") != "recommendation": continue
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


def main():
    os.umask(0o027)
    try: result,code=once(config_read(CONFIG),CONFIG)
    except (OSError,ValueError,PolicyStorageError):
        result,code={"status":"unavailable","reason":"configuration_or_policy_unavailable"},1
    print(json.dumps(result,ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
