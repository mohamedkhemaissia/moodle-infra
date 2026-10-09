"""Un worker local serialise les tests autorises ; aucune restauration en production."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import sys
import time

from backup_status.catalog import select_point
from pilotage.recovery_execution import RecoveryExecutionStore, result_document
from pilotage_backup.core import BackupError, atomic_json, lock, read_file, json_value
from .runtime import Commands, ROOT, cleanup, job_directory, load_config

CONFIG = '/opt/moodle-recovery-tests/config.json'
PROJECT = '/opt/moodle-recovery-tests/recovery_tests/project'
PYTHON = '/opt/moodle-diagnostics/venv/bin/python'
RESULTS = ROOT.parent / 'results'
RUNNER = ROOT.parent / 'runner'


def store_for(config):
    def selector(scope, snapshot, now):
        return select_point(config['catalog_file'],now,scope,snapshot)
    return RecoveryExecutionStore(database_config=config['database_config'],selector=selector)


def proof_failed(reason, flags):
    return {'status':'inconclusive','production_changed':False,'reason':reason,'cleanup':flags}


def run_job(store, job, config, *, runner=None, root=ROOT, results=RESULTS, runner_root=RUNNER, clean=cleanup, cancelled=lambda: False):
    """Un seul appel Runner ; un echec ou une interruption ne relance jamais le test."""
    runner_root=Path(runner_root); results=Path(results)
    interrupted=False; runner_status=None; runner_rc=None
    result=proof_failed('isolated_runner_unavailable',dict.fromkeys(
        ('containers_removed','networks_removed','temporary_files_removed'),False))
    try:
        directory=job_directory(job['id'],root=root)
        directory.mkdir(mode=0o700)
        atomic_json(directory/'job.json',{key:job[key] for key in ('id','scope','snapshot_id','point')})
        private=runner_root/job['id'];private.mkdir(mode=0o700)
        if runner is None:
            import ansible_runner
            runner=ansible_runner.run
        deadline=time.monotonic()+min(config['run_seconds']+30,960)
        if cancelled():raise KeyboardInterrupt()
        outcome=runner(private_data_dir=str(private),project_dir=PROJECT,playbook='test-restore.yml',
            inventory={'all':{'hosts':{'localhost':{'ansible_connection':'local','ansible_python_interpreter':PYTHON}}}},
            extravars={'recovery_test_job_id':job['id']},
            envvars={'PYTHONPATH':'/opt/moodle-recovery-tests','PYTHONDONTWRITEBYTECODE':'1',
                'ANSIBLE_CONFIG':PROJECT+'/ansible.cfg','ANSIBLE_PYTHON_INTERPRETER':PYTHON,
                'PATH':str(Path(PYTHON).parent)+':/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'},
            settings={'pexpect_timeout':1,'job_timeout':min(config['run_seconds']+30,960),'idle_timeout':min(config['run_seconds']+30,960)},
            cancel_callback=lambda: cancelled() or time.monotonic()>=deadline,
            quiet=True)
        runner_status=outcome.status;runner_rc=outcome.rc
        interrupted=cancelled() or runner_status in ('timeout','canceled')
        result=result_document(json_value(read_file(results/(job['id']+'.json'),65536,protected=True)))
        if runner_status!='successful' or runner_rc!=0:
            result['status']='inconclusive';result['reason']='isolated_runner_failed'
    except (KeyboardInterrupt,SystemExit):
        interrupted=True
        result['reason']='isolated_runner_interrupted'
    except Exception:
        # Pas de trace contenant configuration, sorties Restic ou donnees restaurees.
        pass
    finally:
        flags=clean(job['id'],root=root)
        result['cleanup']=flags
        if not all(flags.values()):
            result['status']='inconclusive';result['reason']='isolated_test_cleanup_incomplete'
        if runner_status in ('successful','failed','timeout','canceled'):
            result['runner_status']=runner_status
        if type(runner_rc) is int and -255<=runner_rc<=255:result['runner_rc']=runner_rc
        atomic_json(results/(job['id']+'.json'),result_document(result))
    return store.finish(job['id'],job['claim_token'],result,interrupted=interrupted)


def process(store, config, boot_id, *, run=run_job, clean=cleanup, cancelled=lambda:False):
    for job in store.cleanup_pending():
        flags=clean(job['id'])
        store.record_cleanup(job['id'],flags)
        if not all(flags.values()):return {'status':'cleanup_pending','production_changed':False}
    orphaned=store.abandoned(boot_id)
    for job in orphaned:
        flags=clean(job['id'])
        store.finish(job['id'],job['claim_token'],proof_failed('isolated_runner_interrupted',flags),interrupted=True)
        if not all(flags.values()):return {'status':'cleanup_pending','production_changed':False}
    if cancelled():return {'status':'waiting','production_changed':False}
    job=store.claim(boot_id)
    if job is None:return {'status':'idle','production_changed':False}
    result=run(store,job,config,cancelled=cancelled)
    return {'status':result['status'],'execution_id':job['id'],'production_changed':False}


def main():
    if os.geteuid()!=0:print('Worker local requis.',file=sys.stderr);return 2
    os.umask(0o077)
    stopping=[False]
    def stop(*_):stopping[0]=True
    signal.signal(signal.SIGTERM,stop)
    try:
        config=load_config(CONFIG)
        with ExitStack() as locks:
            for path in ('/run/lock/moodle-recovery-test.lock','/run/lock/moodle-pfe-recovery.lock',
                         '/run/lock/moodle-pilotage-backup.lock','/run/lock/'+config['moodle_project']+'-backup.lock'):
                locks.enter_context(lock(path))
            if (Path(config['moodle_base'])/'backups/restoration-active').exists():
                raise BackupError('backup_or_restore_in_progress')
            boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip().replace('-','')
            result=process(store_for(config),config,boot_id,cancelled=lambda:stopping[0])
        print(json.dumps(result));return 0
    except BackupError as exc:
        # Une sauvegarde en cours garde la demande en attente, sans consommer son execution.
        if str(exc)=='backup_or_restore_in_progress':print(json.dumps({'status':'waiting','production_changed':False}));return 0
        print(json.dumps({'status':'unavailable','production_changed':False}));return 2
    except Exception:
        print(json.dumps({'status':'unavailable','production_changed':False}));return 2


if __name__=='__main__':sys.exit(main())
