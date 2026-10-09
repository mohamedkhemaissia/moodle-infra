"""Processus bornes et nettoyage reserve aux ressources marquees du test."""
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time

from pilotage_backup.core import BackupError, json_value, read_file

DOCKER='/usr/bin/docker'
LABEL='moodle.pfe.recovery-test'
ROOT=Path('/var/lib/moodle-recovery-tests/runs')


class Commands:
    def __init__(self,seconds=960,*,clock=time.monotonic):
        self.clock=clock; self.deadline=clock()+seconds

    def run(self,args,*,stdin=None,output=None,timeout=None,max_bytes=8*1024*1024):
        args=list(args)
        if args and args[0].startswith('/usr/local/sbin/') and args[0].endswith('-restic'):
            args=[args[0],'--no-cache','--no-lock']+[item for item in args[1:] if item not in ('--no-cache','--no-lock')]
        if output is not None and max_bytes==8*1024*1024:max_bytes=512*1024*1024
        remaining=min(timeout or 300,self.deadline-self.clock())
        if remaining<=0:raise BackupError('test_deadline_exceeded')
        process=None
        with tempfile.TemporaryFile() as capture:
            destination=output if output is not None else capture
            try:
                process=subprocess.Popen(args,stdin=stdin or subprocess.DEVNULL,stdout=destination,
                    stderr=subprocess.DEVNULL,start_new_session=True,
                    env={'PATH':'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8'})
                deadline=self.clock()+remaining
                while process.poll() is None:
                    if self.clock()>=deadline:raise BackupError('test_command_timeout')
                    if os.fstat(destination.fileno()).st_size>max_bytes:raise BackupError('test_output_too_large')
                    time.sleep(.025)
                if process.returncode!=0:raise BackupError('test_command_failed')
                if self.clock()>deadline:raise BackupError('test_command_timeout')
                if os.fstat(destination.fileno()).st_size>max_bytes:raise BackupError('test_output_too_large')
                if output is not None:return b''
                capture.seek(0);return capture.read(max_bytes+1)
            except OSError:raise BackupError('test_command_unavailable') from None
            finally:
                if process is not None:
                    for sig in (signal.SIGTERM,signal.SIGKILL):
                        try:os.killpg(process.pid,sig)
                        except ProcessLookupError:pass
                    try:process.wait(timeout=3)
                    except subprocess.TimeoutExpired:raise BackupError('test_process_cleanup_failed') from None


def job_directory(identifier,*,root=ROOT):
    if not isinstance(identifier,str) or not re.fullmatch('[0-9a-f]{32}',identifier):
        raise BackupError('invalid_test_identifier')
    root=Path(root)
    if not root.is_absolute() or any(p.is_symlink() for p in (root,*root.parents)):
        raise BackupError('unsafe_test_directory')
    result=root/identifier
    if result.is_symlink():raise BackupError('unsafe_test_directory')
    return result


def cleanup(identifier,commands=None,*,root=ROOT):
    commands=commands or Commands(120)
    directory=job_directory(identifier,root=root)
    flags={'containers_removed':False,'networks_removed':False,'temporary_files_removed':False}
    try:
        for kind,flag in (('container','containers_removed'),('network','networks_removed')):
            listing=[DOCKER,kind,'ls']+(['--all'] if kind=='container' else [])+['--quiet','--filter','label='+LABEL+'='+identifier]
            listed=commands.run(listing).decode().split()
            if len(listed)>4 or any(not re.fullmatch('[0-9a-f]{12,64}',item) for item in listed):
                raise BackupError('test_resource_inventory_invalid')
            for item in listed:
                info=json_value(commands.run([DOCKER,kind,'inspect',item]))
                if not isinstance(info,list) or len(info)!=1:raise BackupError('test_resource_inventory_invalid')
                labels=info[0].get('Labels',{}) if kind=='network' else info[0].get('Config',{}).get('Labels',{})
                if not isinstance(labels,dict) or labels.get(LABEL)!=identifier:
                    raise BackupError('test_resource_label_mismatch')
                args=[DOCKER,kind,'rm']+(['--force'] if kind=='container' else [])+[item]
                commands.run(args,timeout=30)
            # Confirmer l'absence apres les suppressions, pas seulement leur code retour.
            remaining=commands.run(listing).strip()
            if remaining:raise BackupError('test_resource_cleanup_incomplete')
            flags[flag]=True
        if directory.exists():
            if not directory.is_dir():raise BackupError('unsafe_test_directory')
            shutil.rmtree(directory)
        flags['temporary_files_removed']=not directory.exists()
    except (BackupError,OSError,ValueError,TypeError,KeyError):
        pass
    return flags


def load_config(path):
    value=json_value(read_file(path,protected=True))
    required={'database_config','catalog_file','moodle_base','moodle_project','pilotage_backup_config','run_seconds','min_free_mb'}
    if not isinstance(value,dict) or set(value)!=required:raise BackupError('invalid_worker_configuration')
    for key,prefix in (('database_config','/etc/'),('catalog_file','/var/lib/'),('moodle_base','/opt/'),('pilotage_backup_config','/opt/')):
        item=value[key]
        if not isinstance(item,str) or not re.fullmatch('/[a-zA-Z0-9_./-]+',item) or '..' in item.split('/') or not item.startswith(prefix):
            raise BackupError('invalid_worker_configuration')
        if any(p.is_symlink() for p in (Path(item),*Path(item).parents)):raise BackupError('invalid_worker_configuration')
    if not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}',value['moodle_project']):raise BackupError('invalid_worker_configuration')
    if type(value['run_seconds']) is not int or not 60<=value['run_seconds']<=960:
        raise BackupError('invalid_worker_configuration')
    if type(value['min_free_mb']) is not int or not 512<=value['min_free_mb']<=16384:
        raise BackupError('invalid_worker_configuration')
    return value
