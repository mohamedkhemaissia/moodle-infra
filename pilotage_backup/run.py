"""Exporter le pilotage vers Restic ou verifier une restauration sans toucher au pilote."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import uuid

from .core import (BackupError, Commands, CONFIG_FILES, SERVER_FILES, DatabaseSnapshot,
                   IMAGE_RE, SNAPSHOT_RE, atomic_json, database_config, json_value,
                   load_config, lock, parse_utc, read_file, sha_file, utc_now)
from .inventory import identities as expected_identities, fingerprint, identity_check_sql, inventory_sql, validate_inventory

FORMAT='moodle-pilotage-backup-v1'
TAG='moodle-pilotage-database'
PROJECT='moodle-control-plane-db'
DOCKER='/usr/bin/docker'
FILES=('pilotage.pgdump','inventory.json',*(f'client/{p}' for p in CONFIG_FILES),
       *(f'server/{p}' for p in SERVER_FILES))


def source_container(config,commands,port):
    value=commands.run([DOCKER,'compose','-p',PROJECT,'-f',config['source_base']+'/compose.yml',
                        'ps','--all','--quiet','postgres']).decode().strip()
    if not SNAPSHOT_RE.fullmatch(value):raise BackupError('source_container_not_unique')
    info=json_value(commands.run([DOCKER,'inspect','--format','{{json .}}',value]))
    state=info.get('State',{})
    labels=info.get('Config',{}).get('Labels',{})
    if labels.get('com.docker.compose.project')!=PROJECT or labels.get('com.docker.compose.service')!='postgres':
        raise BackupError('wrong_database_container')
    if state.get('Running') is not True or state.get('Paused') or state.get('Restarting') or state.get('Health',{}).get('Status')!='healthy':
        raise BackupError('source_database_unhealthy')
    image=info.get('Image','')
    if not isinstance(image,str) or not IMAGE_RE.fullmatch(image):raise BackupError('invalid_source_image')
    mounts=info.get('Mounts',[])
    if not any(m.get('Source')==config['source_base']+'/data' and m.get('Destination')=='/var/lib/postgresql/data' for m in mounts):
        raise BackupError('wrong_database_data_directory')
    if info.get('NetworkSettings',{}).get('Ports',{}).get('5432/tcp')!=[{'HostIp':'127.0.0.1','HostPort':str(port)}]:
        raise BackupError('database_port_binding_mismatch')
    return value,image


def copy_configuration(config,directory):
    for kind,base,files in [('client',config['source_config'],CONFIG_FILES),('server',config['source_base'],SERVER_FILES)]:
        for relative in files:
            data=read_file(Path(base)/relative,65536,protected=True)
            if not data:raise BackupError('empty_configuration_file')
            dest=directory/kind/relative;dest.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            with open(dest,'xb') as stream:stream.write(data)
            dest.chmod(0o600)
    if read_file(directory/'client/app_password')!=read_file(directory/'server/secrets/app_password'):
        raise BackupError('database_secrets_do_not_match')
    for relative in ('server/secrets/bootstrap_password','server/secrets/app_password','client/worker-token'):
        if not SNAPSHOT_RE.fullmatch(read_file(directory/relative).decode().strip()):
            raise BackupError('invalid_secret_format')


def restic_snapshot(commands,config,snapshot):
    if not isinstance(snapshot,str) or not SNAPSHOT_RE.fullmatch(snapshot):raise BackupError('invalid_snapshot_id')
    rows=json_value(commands.run([config['restic'],'snapshots','--json',snapshot]))
    if not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict):
        raise BackupError('snapshot_not_unique')
    row=rows[0]
    tags=row.get('tags')
    if row.get('id')!=snapshot or not isinstance(tags,list) or TAG not in tags:raise BackupError('wrong_snapshot_scope')
    paths=row.get('paths',[])
    prefix=config['output_dir']+'/staging/'
    if not isinstance(paths,list) or len(paths)!=1 or not isinstance(paths[0],str) or not paths[0].startswith(prefix):
        raise BackupError('wrong_snapshot_path')
    suffix=paths[0][len(prefix):]
    if not re.fullmatch('run-[a-zA-Z0-9_-]+',suffix):raise BackupError('wrong_snapshot_path')
    return paths[0]


def check_manifest(value):
    if not isinstance(value,dict) or set(value)!={'format','captured_at','dump_completed_at','database','server_major','source_image','inventory_sha256','files'}:
        raise BackupError('invalid_manifest')
    if value['format']!=FORMAT or value['database']!='moodle_pilotage' or value['server_major']!=16:
        raise BackupError('wrong_backup_format')
    if not isinstance(value['source_image'],str) or not IMAGE_RE.fullmatch(value['source_image']):
        raise BackupError('invalid_backup_image')
    if parse_utc(value['captured_at'])>parse_utc(value['dump_completed_at']):raise BackupError('invalid_backup_dates')
    if not isinstance(value['inventory_sha256'],str) or not SNAPSHOT_RE.fullmatch(value['inventory_sha256']):
        raise BackupError('invalid_inventory_fingerprint')
    if not isinstance(value['files'],dict) or set(value['files'])!=set(FILES):raise BackupError('incomplete_backup_files')
    for entry in value['files'].values():
        if not isinstance(entry,dict) or set(entry)!={'sha256','bytes'} or type(entry['bytes']) is not int or entry['bytes']<=0:
            raise BackupError('invalid_file_proof')
        if not isinstance(entry['sha256'],str) or not SNAPSHOT_RE.fullmatch(entry['sha256']):raise BackupError('invalid_file_proof')
        if entry['bytes']>1024*1024*1024:raise BackupError('backup_file_too_large_for_test')
    return value


def backup(config,commands=None,snapshot_factory=DatabaseSnapshot):
    commands=commands or Commands(config['timeout_seconds'])
    root=Path(config['output_dir']);started=utc_now()
    if shutil.disk_usage(root).free<config['min_free_mb']*1024*1024:raise BackupError('insufficient_free_space')
    connection=database_config(config['source_config'])
    container,image=source_container(config,commands,connection['port'])
    # La verification distante precede l'ouverture d'un instantane SQL.
    commands.run([config['restic'],'cat','config'])
    with tempfile.TemporaryDirectory(prefix='run-',dir=root/'staging') as temporary:
        directory=Path(temporary)
        copy_configuration(config,directory)
        with snapshot_factory(connection,config['timeout_seconds']) as snapshot:
            with open(directory/'pilotage.pgdump','xb') as output:
                commands.run([DOCKER,'exec','--user','999:999',container,'/bin/sh','-ec',
                    'export PGPASSWORD="$(cat /run/secrets/app_password)"; export PGCONNECT_TIMEOUT=10; '
                    'exec pg_dump --host=127.0.0.1 --username=pilotage --dbname=moodle_pilotage '
                    '--format=custom --no-acl --lock-wait-timeout=5s --snapshot="$1"',
                    'sh',snapshot.snapshot_id],output=output)
            with open(directory/'pilotage.pgdump','rb') as stream:
                if stream.read(5)!=b'PGDMP':raise BackupError('invalid_postgresql_archive')
            with open(directory/'pilotage.pgdump','rb') as source:
                commands.run([DOCKER,'exec','-i','--user','999:999',container,'pg_restore','--list'],stdin=source)
            inventory=validate_inventory(snapshot.inventory)
            atomic_json(directory/'inventory.json',inventory)
            captured=snapshot.captured_at
        manifest={'format':FORMAT,'captured_at':captured,'dump_completed_at':utc_now(),
                  'database':'moodle_pilotage','server_major':16,'source_image':image,
                  'inventory_sha256':fingerprint(inventory),'files':{p:{'sha256':sha_file(directory/p),
                  'bytes':(directory/p).stat().st_size} for p in FILES}}
        check_manifest(manifest);atomic_json(directory/'manifest.json',manifest)
        raw=commands.run([config['restic'],'backup','--json','--tag',TAG,str(directory)])
        summaries=[json_value(line) for line in raw.splitlines() if line.strip()]
        ids=[r.get('snapshot_id') for r in summaries if isinstance(r,dict) and r.get('message_type')=='summary']
        if len(ids)!=1 or not isinstance(ids[0],str) or not SNAPSHOT_RE.fullmatch(ids[0]):
            raise BackupError('restic_snapshot_not_confirmed')
        sid=ids[0]
        if restic_snapshot(commands,config,sid)!=str(directory):raise BackupError('snapshot_path_mismatch')
        commands.run([config['restic'],'check'])
        result={'status':'backed_up','format':FORMAT,'snapshot':sid,'started_at':started,
                'captured_at':captured,'completed_at':utc_now(),'source_path':str(directory),
                'manifest_sha256':sha_file(directory/'manifest.json'),'tables':len(inventory['tables']),
                'rows':sum(t['rows'] for t in inventory['tables'].values()),
                'repository_check':'passed','restoration':'not_tested'}
        # Publication uniquement apres confirmation du point distant et du check.
        atomic_json(root/'last-success.json',result)
        atomic_json(root/'reports'/('backup-'+uuid.uuid4().hex+'.json'),result)
        return result


def restored_files(config,commands,snapshot,directory):
    source=restic_snapshot(commands,config,snapshot)
    raw=commands.run([config['restic'],'dump',snapshot,source+'/manifest.json'])
    if len(raw)>65536:raise BackupError('manifest_too_large')
    manifest=check_manifest(json_value(raw))
    atomic_json(directory/'manifest.json',manifest)
    for relative,proof in manifest['files'].items():
        target=directory/relative;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if shutil.disk_usage(directory).free-proof['bytes']<config['min_free_mb']*1024*1024:
            raise BackupError('insufficient_restore_space')
        with open(target,'xb') as stream:commands.run([config['restic'],'dump',snapshot,source+'/'+relative],output=stream)
        if target.stat().st_size!=proof['bytes'] or sha_file(target)!=proof['sha256']:
            raise BackupError('restored_file_checksum_mismatch')
        target.chmod(0o600)
    inventory=validate_inventory(json_value(read_file(directory/'inventory.json',512*1024)))
    if fingerprint(inventory)!=manifest['inventory_sha256']:raise BackupError('inventory_checksum_mismatch')
    return manifest,inventory


def restore_test(config,snapshot,commands=None,*,sleep=time.sleep,monotonic=time.monotonic):
    commands=commands or Commands(config['timeout_seconds'])
    root=Path(config['output_dir']);started=utc_now();start_clock=monotonic()
    run_id=uuid.uuid4().hex;name='moodle-pilotage-restore-'+run_id;created=False
    directory=Path(tempfile.mkdtemp(prefix='restore-',dir=root/'staging'))
    result={'status':'failed','mode':'isolated_restore_test','run_id':run_id,'snapshot':snapshot,
            'started_at':started,'production_changed':False}
    try:
        manifest,expected=restored_files(config,commands,snapshot,directory)
        image=manifest['source_image']
        commands.run([DOCKER,'image','inspect',image])
        data=directory/'test-data';data.mkdir(mode=0o700);os.chown(data,999,999)
        # Ne publier aucun port, ne monter aucun repertoire de la base active.
        # Le reseau none rend l'authentification locale de cette fixture inaccessible.
        try:
            commands.run([DOCKER,'run','--detach','--pull','never','--name',name,
                '--label','moodle.pfe.restore-run='+run_id,'--network','none','--user','999:999',
                '--memory','256m','--cpus','0.50','--pids-limit','128','--read-only',
                '--cap-drop','ALL','--security-opt','no-new-privileges:true',
                '--tmpfs','/tmp:rw,noexec,nosuid,size=32m,uid=999,gid=999,mode=1777',
                '--tmpfs','/var/run/postgresql:rw,nosuid,size=8m,uid=999,gid=999,mode=0755',
                '--mount','type=bind,src='+str(data)+',dst=/var/lib/postgresql/data',
                '--env','POSTGRES_DB=moodle_pilotage','--env','POSTGRES_HOST_AUTH_METHOD=trust',image])
            created=True
        except BackupError:
            # Un docker run peut avoir cree le conteneur avant un delai expire.
            created=True
            raise
        deadline=monotonic()+min(180,config['timeout_seconds'])
        while True:
            try:
                commands.run([DOCKER,'exec','--user','999:999',name,'pg_isready','-h','127.0.0.1','-U','postgres','-d','moodle_pilotage'],timeout=15)
                break
            except BackupError:
                if monotonic()>=deadline:raise BackupError('isolated_database_not_ready') from None
                sleep(1)
        # Reconstituer le compte applicatif, sans donner de droits superuser.
        secret=read_file(directory/'client/app_password',4096,protected=True).decode().strip()
        if not SNAPSHOT_RE.fullmatch(secret):raise BackupError('invalid_restored_application_secret')
        role_sql=("BEGIN; CREATE ROLE pilotage LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                  "NOREPLICATION NOBYPASSRLS PASSWORD '"+secret+"'; "
                  "ALTER DATABASE moodle_pilotage OWNER TO pilotage; "
                  "REVOKE CREATE ON SCHEMA public FROM PUBLIC; COMMIT;")
        with tempfile.TemporaryFile() as source:
            source.write(role_sql.encode());source.seek(0)
            commands.run([DOCKER,'exec','-i','--user','999:999',name,'psql','-X','-U','postgres',
                          '-d','moodle_pilotage','-v','ON_ERROR_STOP=1'],stdin=source)
        with open(directory/'pilotage.pgdump','rb') as source:
            commands.run([DOCKER,'exec','-i','--user','999:999',name,'pg_restore','--username=postgres',
                '--dbname=moodle_pilotage','--role=pilotage','--no-owner','--no-acl','--single-transaction','--exit-on-error'],stdin=source)
        def query(statement):
            raw=commands.run([DOCKER,'exec','--user','999:999',name,'psql','-X','-A','-t','-U','pilotage',
                '-d','moodle_pilotage','-v','ON_ERROR_STOP=1','-c','SET statement_timeout=120000; '+statement])
            lines=[line for line in raw.decode().splitlines() if line.strip() and line.strip()!='SET']
            if len(lines)!=1:raise BackupError('invalid_restore_query_response')
            return json_value(lines[0])
        include_recovery = 'pilotage_recovery' in expected['schemas']
        actual=validate_inventory(query(inventory_sql(include_recovery)))
        if actual!=expected:raise BackupError('restored_tables_do_not_match_snapshot')
        identities=query(identity_check_sql(include_recovery))
        if not isinstance(identities,dict) or set(identities)!=set(expected_identities(include_recovery)) or not all(v is True for v in identities.values()):
            raise BackupError('restored_sequences_invalid')
        result.update(status='restore_validated',captured_at=manifest['captured_at'],
            tables_verified=len(actual['tables']),rows_verified=sum(v['rows'] for v in actual['tables'].values()),
            inventory_sha256=fingerprint(actual),sequence_check='passed',
            duration_seconds=round(monotonic()-start_clock,3))
    except (BackupError,ValueError,OSError,TypeError,KeyError) as exc:
        result['reason']=str(exc) if isinstance(exc,BackupError) else 'invalid_backup_or_test_environment'
    finally:
        cleanup_ok=True
        if created:
            try:
                labels=json_value(commands.run([DOCKER,'inspect','--format','{{json .Config.Labels}}',name]))
                if not isinstance(labels,dict) or labels.get('moodle.pfe.restore-run')!=run_id:
                    raise BackupError('restore_container_label_mismatch')
                commands.run([DOCKER,'rm','--force',name],timeout=30)
            except BackupError:cleanup_ok=False
        # Garder le dossier prive si le conteneur ne peut pas etre nettoye.
        if cleanup_ok:
            try:shutil.rmtree(directory)
            except OSError:cleanup_ok=False
        result['cleanup']={'container_removed':cleanup_ok,'temporary_files_removed':cleanup_ok}
        if not cleanup_ok:
            result['status']='inconclusive';result['reason']='isolated_test_cleanup_incomplete'
        result['completed_at']=utc_now()
        report=root/'reports'/('restore-'+run_id+'.json');result['report_file']=str(report)
        atomic_json(report,result)
    return result


def status(config):
    marker=Path(config['output_dir'])/'last-success.json'
    try:
        value=json_value(read_file(marker,protected=True))
        if value.get('format')!=FORMAT or not isinstance(value.get('snapshot'),str) or not SNAPSHOT_RE.fullmatch(value['snapshot']):
            raise ValueError('marker')
        captured=parse_utc(value['captured_at']);completed=parse_utc(value['completed_at']);now=datetime.now(timezone.utc)
        if captured>completed or completed>now:raise ValueError('clock')
        return {**value,'age_hours':round((now-captured).total_seconds()/3600,3),
                'interpretation':'Une sauvegarde confirmee ne prouve pas une restauration.'}
    except (BackupError,ValueError,KeyError,TypeError):return {'status':'unavailable','reason':'no_valid_success_marker'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('backup','status','test-restore'))
    parser.add_argument('--config',default='/opt/moodle-pilotage-backup/config.json')
    parser.add_argument('--snapshot')
    args=parser.parse_args(argv)
    try:
        if os.geteuid()!=0:raise BackupError('root_required')
        os.umask(0o077)
        config=load_config(args.config)
        if args.action=='status':result=status(config)
        else:
            with ExitStack() as stack:
                stack.enter_context(lock('/run/lock/moodle-pilotage-backup.lock'))
                stack.enter_context(lock('/run/lock/'+config['moodle_project']+'-backup.lock'))
                if Path('/opt/moodle/backups/restoration-active').exists():raise BackupError('moodle_restore_in_progress')
                if args.action=='backup':result=backup(config)
                else:
                    sid=args.snapshot or status(config).get('snapshot')
                    if not isinstance(sid,str) or not SNAPSHOT_RE.fullmatch(sid):raise BackupError('explicit_valid_snapshot_required')
                    result=restore_test(config,sid)
        print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
        return 0 if result['status'] in ('backed_up','restore_validated') or (args.action=='status' and result['status']!='unavailable') else 2
    except (BackupError,ValueError,OSError,TypeError,KeyError) as exc:
        result={'status':'blocked','reason':str(exc) if isinstance(exc,BackupError) else 'backup_configuration_or_command_failed',
                'production_changed':False}
        print(json.dumps(result,indent=2));return 2


if __name__=='__main__':sys.exit(main())
