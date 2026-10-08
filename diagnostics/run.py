"""Lancer un diagnostic autorise via Runner et conserver un audit normalise."""
import argparse
import fcntl
import json
import os
import signal
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .collector import ProbeError, get_json, load_config
from .policy import Blocked, validate
from .store import DiagnosticStore, DiagnosticStorageError, strict_json

CODE_DIR = Path(__file__).resolve().parent.parent
CONFIG = CODE_DIR.parent / "config.json"


def execute(config, config_path, incident_id, *, project_dir=None, runner_function=None):
    identifier = uuid.uuid4().hex
    directory = Path(config["data_dir"])
    store = DiagnosticStore(directory)
    # Un seul diagnostic a la fois. Le verrou n'est pas dans le stockage des incidents.
    with (directory / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy", "reason": "diagnostic_already_running"}, 2
        store.start(identifier, incident_id, time.time())
        status, decision, report, events, rc, runner_status = "failed", None, None, [], None, None
        try:
            decision = validate(get_json(config["api_url"] + "/api/v1/decisions/" + str(incident_id)),
                                incident_id, time.time())
            if runner_function is None:
                import ansible_runner
                runner_function = ansible_runner.run
            collected = []
            def event_handler(event):
                data = event.get("event_data", {})
                name = event.get("event", "")
                if name in ("runner_on_ok", "runner_on_failed", "runner_on_unreachable", "playbook_on_stats") and len(events) < 30:
                    events.append({"event": name, "task": str(data.get("task", ""))[:100],
                                   "host": str(data.get("host", ""))[:60]})
                if name == "runner_on_ok" and data.get("task") == "Collecter les preuves en lecture seule":
                    stdout = data.get("res", {}).get("stdout")
                    if isinstance(stdout, str):
                        collected.append(strict_json(stdout))
                return True
            with tempfile.TemporaryDirectory(prefix="runner-", dir=directory) as private:
                os.chmod(private, 0o700)
                expected = Path(private) / "expected.json"
                expected.write_text(json.dumps(decision))
                runner = runner_function(
                    private_data_dir=private, project_dir=str(project_dir or CODE_DIR / "diagnostics" / "project"),
                    playbook="diagnose.yml", inventory={"all": {"hosts": {"local": {"ansible_connection": "local",
                        "ansible_python_interpreter": sys.executable}}}},
                    extravars={"diagnostic_python": sys.executable, "diagnostic_config": str(config_path),
                               "diagnostic_incident_id": incident_id, "diagnostic_expected": str(expected)},
                    envvars={"PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
                             "PILOTAGE_DATABASE_CONFIG": os.environ.get("PILOTAGE_DATABASE_CONFIG", ""),
                             "PYTHONPATH": str(CODE_DIR), "ANSIBLE_CONFIG": str(CODE_DIR / "diagnostics" / "project" / "ansible.cfg"),
                             "ANSIBLE_NOCOLOR": "1", "ANSIBLE_LOCAL_TEMP": private + "/tmp"},
                    settings={"job_timeout": 120, "idle_timeout": 120}, quiet=True,
                    event_handler=event_handler, process_isolation=False)
                rc, runner_status = runner.rc, runner.status
                if runner_status in ("timeout", "canceled"):
                    status = runner_status
                elif rc == 0 and runner_status == "successful" and len(collected) == 1 and isinstance(collected[0], dict) and collected[0].get("status") in ("completed", "blocked"):
                    report = collected[0]
                    status = report["status"]
                    if status == "completed":
                        decision = report["decision"]
                else:
                    report = {"reason": "runner_failed_or_invalid_report"}
        except (Blocked, ProbeError) as exc:
            status = "blocked"
            report = {"reason": "preflight_revalidation_failed", "error_type": type(exc).__name__}
        except KeyboardInterrupt:
            status = "canceled"
            report = {"reason": "operator_interrupt"}
        except Exception as exc:
            status = "failed"
            report = {"reason": "execution_failed", "error_type": type(exc).__name__}
        finally:
            store.finish(identifier, status, time.time(), decision=decision, report=report, events=events,
                         runner_rc=rc, runner_status=runner_status)
        return {"status": status, "run_id": identifier, "incident_id": incident_id,
                "mode": "read_only", "runner_status": runner_status, "runner_rc": rc,
                "detail_url": config["api_url"] + "/api/v1/diagnostics/" + identifier}, (0 if status == "completed" else 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incident-id", type=int, required=True)
    args = parser.parse_args()
    if not 1 <= args.incident_id <= 9223372036854775807:
        parser.error("Identifiant d'incident invalide.")
    os.umask(0o027)
    # SIGTERM conserve un audit quand le processus a la possibilite de sortir proprement.
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        result, code = execute(load_config(CONFIG), CONFIG, args.incident_id)
    except (DiagnosticStorageError, OSError, ValueError) as exc:
        print(json.dumps({"status": "unavailable", "reason": "configuration_or_audit_unavailable", "error_type": type(exc).__name__}))
        return 1
    print(json.dumps(result, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
