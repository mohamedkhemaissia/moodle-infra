"""Configuration, fichiers proteges, commandes bornees et instantane PostgreSQL."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from .inventory import inventory_sql, validate_inventory

UTC_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
SNAPSHOT_RE = re.compile('[0-9a-f]{64}')
IMAGE_RE = re.compile('sha256:[0-9a-f]{64}')
CONFIG_FILES = ('database.json','app_password','active','auth-active','worker-token','worker-token.sha256')
SERVER_FILES = ('compose.yml','init.sh','secrets/bootstrap_password','secrets/app_password')


class BackupError(RuntimeError):
    """Codes de raison sans sortie de commande ni valeur de secret."""


def utc_now():
    return datetime.now(timezone.utc).strftime(UTC_FORMAT)


def parse_utc(value):
    if not isinstance(value,str):
        raise ValueError('date')
    result = datetime.strptime(value,UTC_FORMAT).replace(tzinfo=timezone.utc)
    if result.strftime(UTC_FORMAT) != value:
        raise ValueError('date')
    return result


def json_value(data):
    def pairs(items):
        value={}
        for key,item in items:
            if key in value: raise ValueError('duplicate')
            value[key]=item
        return value
    return json.loads(data,object_pairs_hook=pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('number')))


def read_file(path, limit=65536, *, protected=False):
    path=Path(path)
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise BackupError('unsafe_file')
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>limit or (protected and info.st_mode&0o007):
                raise BackupError('unsafe_file')
            data=stream.read(limit+1)
        if len(data)>limit: raise BackupError('file_too_large')
        return data
    except OSError:
        raise BackupError('file_unavailable') from None


def atomic_json(path,value):
    path=Path(path)
    if path.is_symlink() or path.parent.is_symlink(): raise BackupError('unsafe_output')
    fd,name=tempfile.mkstemp(prefix='.pilotage-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)
            stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.chmod(name,0o600)
        os.replace(name,path)
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if os.path.exists(name):os.unlink(name)


def sha_file(path):
    result=hashlib.sha256()
    with open(path,'rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):result.update(block)
    return result.hexdigest()


def absolute_path(value,prefix):
    if not isinstance(value,str) or not re.fullmatch('/[a-zA-Z0-9_./-]+',value) or '..' in value.split('/'):
        raise BackupError('invalid_path')
    if not value.startswith(prefix) or any(Path(p).is_symlink() for p in (Path(value),*Path(value).parents)):
        raise BackupError('invalid_path')
    return value


def load_config(path):
    value=json_value(read_file(path,protected=True))
    if not isinstance(value,dict) or set(value)!={'source_base','source_config','output_dir','restic','moodle_project','timeout_seconds','min_free_mb'}:
        raise BackupError('invalid_configuration')
    for key,prefix in [('source_base','/opt/'),('source_config','/etc/'),('output_dir','/var/lib/'),('restic','/usr/local/sbin/')]:
        absolute_path(value[key],prefix)
    if not isinstance(value['moodle_project'],str) or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}',value['moodle_project']):
        raise BackupError('invalid_project')
    if value['restic']!='/usr/local/sbin/'+value['moodle_project']+'-restic':
        raise BackupError('invalid_restic_wrapper')
    if type(value['timeout_seconds']) is not int or not 30<=value['timeout_seconds']<=1800:
        raise BackupError('invalid_timeout')
    if type(value['min_free_mb']) is not int or not 128<=value['min_free_mb']<=16384:
        raise BackupError('invalid_free_space')
    return value


def database_config(directory):
    value=json_value(read_file(Path(directory)/'database.json',protected=True))
    if not isinstance(value,dict) or set(value)!={'host','port','dbname','user','password_file'}:
        raise BackupError('invalid_database_config')
    if value['host']!='127.0.0.1' or value['dbname']!='moodle_pilotage' or value['user']!='pilotage':
        raise BackupError('invalid_database_target')
    if type(value['port']) is not int or not 1024<=value['port']<=65535:
        raise BackupError('invalid_database_port')
    if value.pop('password_file')!=str(Path(directory)/'app_password'):
        raise BackupError('invalid_database_secret_path')
    password=read_file(Path(directory)/'app_password',4096,protected=True).decode().strip()
    if not re.fullmatch('[a-f0-9]{64}',password):raise BackupError('invalid_database_secret')
    return {**value,'password':password}


@contextmanager
def lock(path):
    fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise BackupError('backup_or_restore_in_progress') from None
        yield
    finally:os.close(fd)


class Commands:
    def __init__(self,timeout=300):self.timeout=timeout
    def run(self,args,*,stdin=None,output=None,timeout=None):
        try:
            result=subprocess.run(args,stdin=stdin,stdout=output if output is not None else subprocess.PIPE,
                                  stderr=subprocess.PIPE,timeout=timeout or self.timeout,check=False,
                                  env={**{k:v for k,v in os.environ.items() if not k.startswith('PG')},
                                       'PATH':'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8'})
        except (OSError,subprocess.TimeoutExpired):
            raise BackupError('command_unavailable_or_timeout') from None
        if result.returncode!=0:raise BackupError('command_failed')
        data=result.stdout if output is None else b''
        if len(data)>2*1024*1024:raise BackupError('command_response_too_large')
        return data


class DatabaseSnapshot:
    def __init__(self,config,timeout=300):self.config,self.timeout=config,timeout
    def __enter__(self):
        import psycopg
        try:
            self.connection=psycopg.connect(**self.config,connect_timeout=5,autocommit=True,
                application_name='moodle-pilotage-backup',options=f'-c statement_timeout={self.timeout*1000} -c lock_timeout=5000')
            self.connection.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
            privileges=self.connection.execute('SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls '
                                               'FROM pg_roles WHERE rolname=current_user').fetchone()
            if privileges!=(False,False,False,False,False):raise BackupError('application_role_privileged')
            self.captured_at=utc_now()
            self.snapshot_id=self.connection.execute('SELECT pg_export_snapshot()').fetchone()[0]
            if not re.fullmatch('[0-9A-F]+-[0-9A-F]+-[0-9]+',self.snapshot_id,re.I):
                raise BackupError('invalid_database_snapshot')
            include_recovery = self.connection.execute("SELECT to_regnamespace('pilotage_recovery')").fetchone()[0] is not None
            include_execution = include_recovery and self.connection.execute("SELECT to_regclass('pilotage_recovery.executions')").fetchone()[0] is not None
            include_policy = self.connection.execute("SELECT to_regnamespace('pilotage_policy')").fetchone()[0] is not None
            self.inventory=validate_inventory(self.connection.execute(inventory_sql(include_recovery,include_execution,include_policy)).fetchone()[0],database=self.config['dbname'])
            return self
        except Exception:
            if hasattr(self,'connection'):self.connection.close()
            raise BackupError('database_snapshot_unavailable') from None
    def __exit__(self,*_):self.connection.close()
