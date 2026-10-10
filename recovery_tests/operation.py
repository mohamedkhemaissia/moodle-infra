"""Commande allowlistee appelee par Ansible Runner ; preuve JSON privee."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys

from pilotage_backup import run as pilotage_run
from pilotage_backup.core import BackupError, atomic_json, load_config as backup_config, read_file, json_value
from pilotage.recovery_execution import result_document
from . import moodle
from .archives import snapshot_source
from .runtime import Commands, ROOT, cleanup, job_directory, load_config


def execute(config,job,*,root=ROOT,commands=None,clean=cleanup):
    directory=job_directory(job['id'],root=root)
    commands=commands or Commands(config['run_seconds'])
    result={'status':'failed','production_changed':False,'reason':'test_environment_unusable'}
    try:
        if not directory.is_dir() or (directory/'archives').exists():raise BackupError('test_directory_not_new')
        if shutil.disk_usage(directory).free<config['min_free_mb']*1024*1024:
            raise BackupError('insufficient_test_space')
        if job['scope']=='moodle':result=moodle.execute(config,job,directory,commands)
        elif job['scope']=='pilotage':
            snapshot_source(config,job,commands)
            selected=backup_config(config['pilotage_backup_config'])
            for name in ('staging','reports'):(directory/name).mkdir(mode=0o700)
            proof=pilotage_run.restore_test(selected,job['snapshot_id'],commands,
                run_id=job['id'],work_root=directory)
            result={key:proof[key] for key in ('status','production_changed','tables_verified','rows_verified','inventory_sha256','sequence_check','reason') if key in proof}
            if proof['status']=='restore_validated':result['database_check']='passed'
        else:raise BackupError('invalid_test_scope')
    except (BackupError,OSError,ValueError,TypeError,KeyError):
        error=sys.exc_info()[1]
        reason=str(error) if isinstance(error,BackupError) else 'invalid_backup_or_test_environment'
        if not isinstance(reason,str) or not __import__('re').fullmatch('[a-z][a-z0-9_]{0,95}',reason):reason='test_environment_unusable'
        result={'status':'failed','production_changed':False,'reason':reason}
    finally:
        result['cleanup']=clean(job['id'],root=root)
        if not all(result['cleanup'].values()):
            result['status']='inconclusive';result['reason']='isolated_test_cleanup_incomplete'
    return result_document(result)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-id',required=True)
    args=parser.parse_args()
    if os.geteuid()!=0:parser.exit(2,'Worker local requis.\n')
    os.umask(0o077)
    directory=job_directory(args.job_id)
    # Le descripteur est prepare par le worker, jamais par une requete HTTP.
    job=json_value(read_file(directory/'job.json',65536,protected=True))
    if not isinstance(job,dict) or set(job)!={'id','scope','snapshot_id','point'} or job['id']!=args.job_id:
        parser.exit(2,'Descripteur du test invalide.\n')
    from pilotage.recovery import identifier
    identifier(job['snapshot_id'],64)
    result=execute(load_config('/opt/moodle-recovery-tests/config.json'),job)
    # cleanup() enleve le dossier du job ; la preuve reste dans un dossier prive distinct.
    atomic_json('/var/lib/moodle-recovery-tests/results/'+args.job_id+'.json',result)
    print(json.dumps({'status':result['status'],'production_changed':False},allow_nan=False))
    return 0


if __name__=='__main__':sys.exit(main())
