import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pilotage_backup import core, run
from pilotage_backup.inventory import TABLES, IDENTITIES, fingerprint, inventory_sql, validate_inventory

# Fixture du schema applicatif migre, independante de la liste de sauvegarde :
# une table oubliee doit faire echouer les tests, meme sur le code installe seul.
SOURCE_INVENTORY_SCHEMAS = {
    'pilotage_incidents': (0x4D4F4F49,
        ('schema_info', 'metadata', 'signal_state', 'incidents', 'events', 'migration_info')),
    'pilotage_diagnostics': (0x4D444941, ('schema_info', 'runs')),
    'pilotage_remediation': (0x4D444941, ('schema_info', 'runs', 'claims')),
    'pilotage_auth': (0x4D4F4155, ('schema_info', 'users', 'sessions', 'login_limits', 'events')),
}
SOURCE_INVENTORY_TABLES = tuple(
    f'{schema}.{table}' for schema, (_, tables) in SOURCE_INVENTORY_SCHEMAS.items() for table in tables)


def valid_inventory():
    return {'database':'moodle_pilotage','server_major':16,
        'schemas':{s:[1,app] for s,(app,_) in SOURCE_INVENTORY_SCHEMAS.items()},'table_names':sorted(SOURCE_INVENTORY_TABLES),
        'tables':{t:{'rows':1,'sha256':hashlib.sha256(t.encode()).hexdigest(),
                     'columns':[['id','int8','NO']]} for t in SOURCE_INVENTORY_TABLES}}


class FakeSnapshot:
    active=False
    snapshot_id='00000003-00000010-1'
    captured_at='2026-10-08T22:00:00Z'
    inventory=valid_inventory()
    def __init__(self,*_):pass
    def __enter__(self):FakeSnapshot.active=True;return self
    def __exit__(self,*_):FakeSnapshot.active=False


class FakeCommands:
    def __init__(self,config):
        self.config=config;self.calls=[];self.archives={};self.source_path='';self.source_info={
            'State':{'Running':True,'Paused':False,'Restarting':False,'Health':{'Status':'healthy'}},
            'Config':{'Labels':{'com.docker.compose.project':run.PROJECT,'com.docker.compose.service':'postgres'}},
            'Image':'sha256:'+'c'*64,
            'Mounts':[{'Source':config['source_base']+'/data','Destination':'/var/lib/postgresql/data'}],
            'NetworkSettings':{'Ports':{'5432/tcp':[{'HostIp':'127.0.0.1','HostPort':'15432'}]}}}
        self.fail_check=False;self.fail_dump=False;self.bad_summary=False;self.corrupt_file=False
        self.restored_inventory=valid_inventory();self.identities={t:True for t in IDENTITIES}
        self.wrong_label=False;self.fail_remove=False;self.scope_tag=run.TAG;self.removed=False
        self.role_sql='';self.restore_args=[]
    def run(self,args,*,stdin=None,output=None,timeout=None):
        self.calls.append(args)
        if args[0]==self.config['restic']:
            command=args[1]
            if command=='cat':data=b'{}'
            elif command=='backup':
                assert not FakeSnapshot.active
                directory=Path(args[-1]);self.source_path=str(directory)
                self.archives={str(p):p.read_bytes() for p in directory.rglob('*') if p.is_file()}
                data=json.dumps({'message_type':'summary','snapshot_id':None if self.bad_summary else 'a'*64}).encode()
            elif command=='snapshots':data=json.dumps([{'id':'a'*64,'tags':[self.scope_tag],'paths':[self.source_path]}]).encode()
            elif command=='dump':
                data=self.archives[args[-1]]
                if self.corrupt_file and args[-1].endswith('pilotage.pgdump'):data+=b'corrupt'
            elif command=='check':
                if self.fail_check:raise core.BackupError('command_failed')
                data=b''
            else:raise AssertionError(args)
        elif args[:2]==[run.DOCKER,'compose']:data=('b'*64+'\n').encode()
        elif args[:2]==[run.DOCKER,'image']:data=b'[]'
        elif args[:2]==[run.DOCKER,'run']:
            self.label=args[args.index('--label')+1].split('=',1)[1];data=('d'*64).encode()
        elif args[:2]==[run.DOCKER,'inspect']:
            data=json.dumps({'moodle.pfe.restore-run':'wrong' if self.wrong_label else self.label}
                if args[3]=='{{json .Config.Labels}}' else self.source_info).encode()
        elif args[:2]==[run.DOCKER,'rm']:
            if self.fail_remove:raise core.BackupError('command_failed')
            self.removed=True;data=b''
        elif args[:2]==[run.DOCKER,'exec']:
            if '/bin/sh' in args:
                assert FakeSnapshot.active
                if self.fail_dump:raise core.BackupError('command_failed')
                data=b'PGDMP fixture for orchestration tests only'
            elif 'pg_restore' in args:
                if '--list' in args:assert FakeSnapshot.active
                else:self.restore_args=args;assert stdin.read().startswith(b'PGDMP')
                data=b''
            elif 'pg_isready' in args:data=b'accepting connections'
            elif 'psql' in args:
                if '-c' in args:
                    value=self.restored_inventory if 'server_major' in args[-1] else self.identities
                    data=('SET\n'+json.dumps(value)+'\n').encode()
                else:self.role_sql=stdin.read().decode();data=b'BEGIN\nCOMMIT\n'
            else:raise AssertionError(args)
        else:raise AssertionError(args)
        if output is not None:output.write(data);return b''
        return data


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.config={'source_base':str(root/'server'),'source_config':str(root/'client'),
            'output_dir':str(root/'output'),'restic':'/usr/local/sbin/moodle-pilote-restic',
            'moodle_project':'moodle-pilote','timeout_seconds':300,'min_free_mb':128}
        self.root=root/'output'
        for path in (self.root,self.root/'staging',self.root/'reports'):path.mkdir(mode=0o700,parents=True,exist_ok=True)
        for kind,base,files in [('client',root/'client',core.CONFIG_FILES),('server',root/'server',core.SERVER_FILES)]:
            for relative in files:
                target=base/relative;target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                data='e'*64+'\n' if 'password' in relative or relative=='worker-token' else 'fixture\n'
                if relative=='database.json':data=json.dumps({'host':'127.0.0.1','port':15432,
                    'dbname':'moodle_pilotage','user':'pilotage','password_file':str(base/'app_password')})
                target.write_text(data);target.chmod(0o600)
        self.commands=FakeCommands(self.config)
        self.chown=patch.object(run.os,'chown');self.chown.start();self.addCleanup(self.chown.stop)
    def backup(self):return run.backup(self.config,self.commands,FakeSnapshot)
    def restore(self):return run.restore_test(self.config,'a'*64,self.commands,sleep=lambda _:None)
    def test_backup_confirms_remote_snapshot_then_publishes_marker_and_erases_staging(self):
        result=self.backup()
        self.assertEqual(result['status'],'backed_up');self.assertEqual(result['tables'],16)
        self.assertEqual(result['restoration'],'not_tested')
        self.assertEqual(json.loads((self.root/'last-success.json').read_text()),result)
        self.assertEqual(list((self.root/'staging').iterdir()),[])
        self.assertEqual(set(Path(p).relative_to(result['source_path']).as_posix() for p in self.commands.archives),
                         set(run.FILES)|{'manifest.json'})
        self.assertEqual((self.root/'last-success.json').stat().st_mode&0o777,0o600)
    def test_all_source_rows_are_proven_from_the_same_exported_snapshot(self):
        self.backup()
        dump=next(args for args in self.commands.calls if '/bin/sh' in args)
        self.assertEqual(dump[-1],FakeSnapshot.snapshot_id)
        self.assertIn('--snapshot="$1"',dump[-3])
        self.assertFalse(FakeSnapshot.active)
    def test_repository_failure_preserves_the_existing_success_marker(self):
        self.backup();before=(self.root/'last-success.json').read_bytes()
        self.commands.fail_check=True
        with self.assertRaises(core.BackupError):self.backup()
        self.assertEqual((self.root/'last-success.json').read_bytes(),before)
        self.assertEqual(list((self.root/'staging').iterdir()),[])
    def test_dump_failure_never_sends_or_marks_a_snapshot(self):
        self.commands.fail_dump=True
        with self.assertRaises(core.BackupError):self.backup()
        self.assertFalse((self.root/'last-success.json').exists())
        self.assertFalse(any(args[1]=='backup' for args in self.commands.calls))
    def test_missing_restic_summary_does_not_publish_success(self):
        self.commands.bad_summary=True
        with self.assertRaises(core.BackupError):self.backup()
        self.assertFalse((self.root/'last-success.json').exists())
    def test_wrong_container_project_port_or_paused_database_is_rejected(self):
        for field in ('project','port','paused'):
            info=copy.deepcopy(self.commands.source_info)
            if field=='project':self.commands.source_info['Config']['Labels']['com.docker.compose.project']='moodle-pilote'
            if field=='port':self.commands.source_info['NetworkSettings']['Ports']['5432/tcp'][0]['HostPort']='5432'
            if field=='paused':self.commands.source_info['State']['Paused']=True
            with self.subTest(field=field),self.assertRaises(core.BackupError):self.backup()
            self.commands.source_info=info
    def test_inconsistent_configured_password_is_rejected_before_dump(self):
        Path(self.config['source_base'],'secrets/app_password').write_text('f'*64+'\n')
        with self.assertRaises(core.BackupError):self.backup()
        self.assertFalse(any('/bin/sh' in args for args in self.commands.calls))
    def test_successful_restore_recreates_the_application_role_and_checks_all_tables(self):
        self.backup();before=(self.root/'last-success.json').read_bytes()
        result=self.restore()
        self.assertEqual(result['status'],'restore_validated');self.assertEqual(result['tables_verified'],16)
        self.assertEqual(result['inventory_sha256'],fingerprint(valid_inventory()))
        self.assertEqual(result['production_changed'],False)
        self.assertTrue(self.commands.removed);self.assertEqual(list((self.root/'staging').iterdir()),[])
        self.assertEqual((self.root/'last-success.json').read_bytes(),before)
        self.assertIn('NOSUPERUSER NOCREATEDB NOCREATEROLE',self.commands.role_sql)
        self.assertIn('--role=pilotage',self.commands.restore_args)
        args=next(a for a in self.commands.calls if a[:2]==[run.DOCKER,'run'])
        self.assertEqual(args[args.index('--network')+1],'none')
        self.assertNotIn('--publish',args);self.assertNotIn('-p',args)
        mount=args[args.index('--mount')+1]
        self.assertIn(str(self.root/'staging'),mount)
        self.assertNotIn(self.config['source_base'],mount)
        self.assertNotIn('e'*64,json.dumps(self.commands.calls))
        self.assertNotIn('e'*64,json.dumps(result))
    def test_restored_file_corruption_blocks_before_any_container_is_created(self):
        self.backup();self.commands.corrupt_file=True
        result=self.restore();self.assertEqual(result['status'],'failed')
        self.assertEqual(result['reason'],'restored_file_checksum_mismatch')
        self.assertFalse(any(a[:2]==[run.DOCKER,'run'] for a in self.commands.calls))
    def test_wrong_restic_scope_or_path_never_restores_foreign_snapshots(self):
        self.backup();self.commands.scope_tag='moodle-pilote-full'
        self.assertEqual(self.restore()['reason'],'wrong_snapshot_scope')
        self.commands.scope_tag=run.TAG;self.commands.source_path='/opt/moodle'
        self.assertEqual(self.restore()['reason'],'wrong_snapshot_path')
    def test_modified_rows_and_invalid_sequences_are_not_reported_as_success(self):
        self.backup();self.commands.restored_inventory['tables'][TABLES[0]]['sha256']='f'*64
        self.assertEqual(self.restore()['reason'],'restored_tables_do_not_match_snapshot')
        self.commands.restored_inventory=valid_inventory();self.commands.identities[IDENTITIES[0]]=False
        self.assertEqual(self.restore()['reason'],'restored_sequences_invalid')
        self.assertTrue(self.commands.removed)
    def test_changed_migration_proof_prevents_restore_validation(self):
        self.backup()
        self.commands.restored_inventory['tables']['pilotage_incidents.migration_info']['sha256']='f'*64
        result=self.restore()
        self.assertEqual(result['reason'],'restored_tables_do_not_match_snapshot')
        self.assertTrue(self.commands.removed)
    def test_cleanup_refuses_a_container_with_the_wrong_run_label(self):
        self.backup();self.commands.wrong_label=True
        result=self.restore();self.assertEqual(result['status'],'inconclusive')
        self.assertFalse(self.commands.removed);self.assertFalse(result['cleanup']['container_removed'])
        self.assertTrue(list((self.root/'staging').iterdir()))
    def test_no_success_is_claimed_if_the_test_container_cannot_be_removed(self):
        self.backup();self.commands.fail_remove=True
        self.assertEqual(self.restore()['status'],'inconclusive')
    def test_manifest_cannot_add_files_outside_the_scoped_bundle(self):
        self.backup();path=self.commands.source_path+'/manifest.json'
        value=json.loads(self.commands.archives[path]);value['files']['../../etc/shadow']=value['files']['inventory.json']
        self.commands.archives[path]=json.dumps(value).encode()
        self.assertEqual(self.restore()['reason'],'incomplete_backup_files')
    def test_status_distinguishes_missing_or_invalid_marker_without_filling_it(self):
        self.assertEqual(run.status(self.config)['status'],'unavailable')
        self.backup();value=run.status(self.config)
        self.assertEqual(value['status'],'backed_up');self.assertGreaterEqual(value['age_hours'],0)
        (self.root/'last-success.json').write_text('bad json')
        self.assertEqual(run.status(self.config)['status'],'unavailable')


class FormatsTests(unittest.TestCase):
    def snapshot_connection(self,privileges=(False,)*5,snapshot='00000003-00000010-1',inventory=None):
        statements=[]
        connection=SimpleNamespace(closed=False)
        def execute(statement):
            statements.append(statement)
            value=privileges if 'rolsuper' in statement else (snapshot,) if 'pg_export_snapshot' in statement \
                else ((inventory if inventory is not None else valid_inventory()),)
            return SimpleNamespace(fetchone=lambda:value)
        connection.execute=execute
        connection.close=lambda:setattr(connection,'closed',True)
        return connection,statements
    def test_source_snapshot_is_read_only_kept_open_and_closed_after_dump(self):
        connection,statements=self.snapshot_connection()
        with patch.dict('sys.modules',{'psycopg':SimpleNamespace(connect=lambda **_:connection)}):
            with core.DatabaseSnapshot({'dbname':'moodle_pilotage'}) as capture:
                self.assertFalse(connection.closed)
                self.assertEqual(capture.snapshot_id,'00000003-00000010-1')
                self.assertEqual(capture.inventory,valid_inventory())
            self.assertTrue(connection.closed)
        self.assertEqual(statements[0],'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
    def test_privileged_source_role_is_rejected_and_connection_closed(self):
        connection,_=self.snapshot_connection(privileges=(True,False,False,False,False))
        with patch.dict('sys.modules',{'psycopg':SimpleNamespace(connect=lambda **_:connection)}):
            with self.assertRaises(core.BackupError):
                with core.DatabaseSnapshot({'dbname':'moodle_pilotage'}):pass
        self.assertTrue(connection.closed)
    def test_invalid_exported_snapshot_and_schema_are_rejected_and_closed(self):
        invalid=valid_inventory();invalid['schemas']['pilotage_auth']=[2,1]
        for overrides in ({'snapshot':'invalid; sql'},{'inventory':invalid}):
            connection,_=self.snapshot_connection(**overrides)
            with self.subTest(overrides=overrides),patch.dict('sys.modules',{'psycopg':SimpleNamespace(connect=lambda **_:connection)}):
                with self.assertRaises(core.BackupError):
                    with core.DatabaseSnapshot({'dbname':'moodle_pilotage'}):pass
            self.assertTrue(connection.closed)
    def test_inventory_rejects_missing_schemas_wrong_versions_and_invalid_counts(self):
        for change in ('missing','version','bool','fingerprint','extra_table'):
            value=valid_inventory()
            if change=='missing':value['schemas'].pop('pilotage_auth')
            if change=='version':value['server_major']=17
            if change=='bool':value['tables'][TABLES[0]]['rows']=True
            if change=='fingerprint':value['tables'][TABLES[0]]['sha256']='not a hash'
            if change=='extra_table':value['table_names'].append('public.not_saved')
            with self.subTest(change=change),self.assertRaises(ValueError):validate_inventory(value)
    def test_migrated_database_includes_migration_proof_in_inventory(self):
        value=valid_inventory()
        self.assertEqual(validate_inventory(value),value)
        self.assertIn('FROM pilotage_incidents.migration_info t',inventory_sql())
        value['table_names'].remove('pilotage_incidents.migration_info')
        del value['tables']['pilotage_incidents.migration_info']
        with self.assertRaises(ValueError):validate_inventory(value)
    def test_configuration_targets_only_the_dedicated_database_and_fixed_wrapper(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'config';path.chmod(0o600) if path.exists() else None
            value={'source_base':'/opt/moodle-control-plane-db','source_config':'/etc/moodle-pilotage',
                'output_dir':'/var/lib/moodle-pilotage-backup','restic':'/usr/local/sbin/moodle-pilote-restic',
                'moodle_project':'moodle-pilote','timeout_seconds':300,'min_free_mb':512}
            path.write_text(json.dumps(value));path.chmod(0o600)
            self.assertEqual(core.load_config(path),value)
            for key,bad in [('source_base','/opt/../moodle'),('restic','/usr/local/sbin/arbitrary-command'),
                            ('timeout_seconds',True),('moodle_project','x; rm'),('min_free_mb',0)]:
                invalid={**value,key:bad};path.write_text(json.dumps(invalid))
                with self.subTest(key=key),self.assertRaises(core.BackupError):core.load_config(path)
    def test_json_rejects_duplicate_keys_and_non_finite_values(self):
        for value in ('{"a":1,"a":2}','{"a":NaN}','{"a":Infinity}'):
            with self.assertRaises(ValueError):core.json_value(value)
    def test_read_refuses_symlinks_fifo_large_files_and_public_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);target=root/'secret';target.write_text('secret');target.chmod(0o600)
            link=root/'link';link.symlink_to(target)
            with self.assertRaises(core.BackupError):core.read_file(link)
            with self.assertRaises(core.BackupError):core.read_file(target,2)
            target.chmod(0o644)
            with self.assertRaises(core.BackupError):core.read_file(target,protected=True)
            fifo=root/'fifo';os.mkfifo(fifo)
            with self.assertRaises(core.BackupError):core.read_file(fifo)
    def test_lock_rejects_overlapping_operations_and_releases_after_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'lock'
            with core.lock(path):
                with self.assertRaises(core.BackupError):
                    with core.lock(path):pass
            with core.lock(path):pass
    def test_sql_is_scoped_to_expected_tables_and_does_not_return_rows_in_clear(self):
        statement=inventory_sql()
        self.assertIn('sha256(convert_to',statement);self.assertIn('COLLATE "C"',statement)
        for table in TABLES:self.assertIn(table,statement)
        self.assertEqual(len(TABLES),16)
    def test_cli_requires_root_and_does_not_display_sensitive_exceptions(self):
        output=io.StringIO()
        with patch.object(core.os,'geteuid',return_value=1),redirect_stdout(output):
            self.assertEqual(run.main(['status']),2)
        self.assertEqual(json.loads(output.getvalue())['reason'],'root_required')
    def test_atomic_report_has_private_permissions_and_refuses_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'report';core.atomic_json(path,{'status':'test'})
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            other=Path(temporary)/'other';other.write_text('preserved')
            path.unlink();path.symlink_to(other)
            with self.assertRaises(core.BackupError):core.atomic_json(path,{'overwrite':True})
            self.assertEqual(other.read_text(),'preserved')


if __name__=='__main__':unittest.main()
