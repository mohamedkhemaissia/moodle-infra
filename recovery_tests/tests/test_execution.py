"""Protections des archives, isolation et parcours Runner sans Docker actif."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pilotage.recovery_execution import result_document
from pilotage_backup.core import BackupError, atomic_json
from recovery_tests import archives, moodle, operation, runtime, worker

ID='a'*32
FLAGS=dict(containers_removed=True,networks_removed=True,temporary_files_removed=True)
GOOD=dict(status='restore_validated',production_changed=False,database_check='passed',cleanup=FLAGS)
CONFIG=dict(moodle_project='moodle-pilote',moodle_base='/opt/moodle',run_seconds=60,min_free_mb=512,
    pilotage_backup_config='/opt/moodle-pilotage-backup/config.json')
JOB=dict(id=ID,scope='moodle',snapshot_id='b'*64,claim_token='c'*32,
    point=dict(id='b'*64,archived_at='2026-10-09T11:00:00Z',hostname='moodleprod'))


def tar(path, members):
    with tarfile.open(path,'w') as output:
        for name,kind,value in members:
            member=tarfile.TarInfo(name)
            if kind=='file':member.size=len(value);output.addfile(member,io.BytesIO(value))
            elif kind=='dir':member.type=tarfile.DIRTYPE;output.addfile(member)
            elif kind in ('sym','hard'):member.type=tarfile.SYMTYPE if kind=='sym' else tarfile.LNKTYPE;member.linkname=value;output.addfile(member)
            else:member.type=tarfile.FIFOTYPE;output.addfile(member)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
    def test_real_tar_regular_files_and_internal_composer_links(self):
        source=self.root/'source.tar';target=self.root/'copy'
        tar(source,[('./','dir',''),('./vendor/file.php','file',b'php'),('vendor/link.php','sym','file.php'),('other.php','hard','vendor/file.php')])
        self.assertEqual(archives.safe_extract(source,target),1)
        self.assertEqual((target/'vendor/link.php').read_bytes(),b'php')
        self.assertEqual((target/'other.php').read_bytes(),b'php')
    def test_traversal_devices_duplicate_and_external_links_are_rejected(self):
        for index,members in enumerate([
            [('../outside','file',b'bad')],[('/etc/passwd','file',b'bad')],[('fifo','device','')],
            [('a','file',b'1'),('a','file',b'2')],[('a','sym','../../opt/moodle')],
            [('a','hard','/opt/moodle/secret')],[('a\\b','file',b'bad')]]):
            source=self.root/f'{index}.tar';tar(source,members)
            with self.subTest(index=index),self.assertRaises(BackupError):archives.safe_extract(source,self.root/f'copy{index}')
    def test_archive_limits_and_configuration_selection_exclude_other_secrets(self):
        source=self.root/'a.tar';tar(source,[('compose.yml','file',b'{}'),('secrets/moodle_db_password','file',b'x'),('secrets/sftp_key','file',b'private')])
        target=self.root/'copy'
        archives.safe_extract(source,target,selected={'compose.yml','secrets/moodle_db_password'})
        self.assertFalse((target/'secrets/sftp_key').exists())
        with self.assertRaises(BackupError):archives.safe_extract(source,self.root/'small',max_bytes=1)
        with self.assertRaises(BackupError):archives.safe_extract(source,self.root/'missing',selected={'missing'})
    def test_snapshot_scope_origin_and_full_identifier_are_revalidated(self):
        valid=dict(id='b'*64,time='2026-10-09T11:00:00.123456789Z',hostname='moodleprod',tags=['moodle-pilote-full'],paths=['/opt/moodle/backups/staging/run-ci'])
        commands=SimpleNamespace(run=lambda *a,**k:json.dumps([valid]).encode())
        self.assertEqual(archives.snapshot_source(CONFIG,JOB,commands)[1],valid['paths'][0])
        for key,value in [('id','b'*12),('hostname','other'),('tags',['pilotage']),('paths',['/opt/moodle/live'])]:
            bad={**valid,key:value};commands.run=lambda *a,**k:json.dumps([bad]).encode()
            with self.subTest(key=key),self.assertRaises(BackupError):archives.snapshot_source(CONFIG,JOB,commands)
    def test_checksum_manifest_is_required_before_any_docker_dispatch(self):
        directory=self.root/'archives';directory.mkdir()
        commands=SimpleNamespace(run=lambda *a,**k:b'not a checksum')
        with patch.object(archives,'snapshot_source',return_value=('/wrapper','/archive')):
            with self.assertRaises(BackupError):archives.fetch_moodle(CONFIG,JOB,directory,commands)
        self.assertEqual(list(directory.iterdir()),[])


class RuntimeTests(unittest.TestCase):
    def test_process_deadline_and_output_limit_are_enforced(self):
        command=runtime.Commands(5)
        with self.assertRaisesRegex(BackupError,'timeout'):
            command.run([sys.executable,'-c','import time; time.sleep(2)'],timeout=.1)
        with self.assertRaisesRegex(BackupError,'too_large'):
            command.run([sys.executable,'-c','print("x"*10000)'],max_bytes=20)
    def test_nonzero_output_and_sensitive_errors_are_not_exposed(self):
        with self.assertRaisesRegex(BackupError,'^test_command_failed$'):
            runtime.Commands(5).run([sys.executable,'-c','import sys;print("secret",file=sys.stderr);sys.exit(1)'])
    def test_cleanup_queries_stopped_containers_and_checks_labels_before_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/ID).mkdir();calls=[]
            def run(args,**kwargs):
                calls.append(args)
                if 'ls' in args:return b'd'*12 if sum('ls' in c for c in calls)==1 else b''
                if 'inspect' in args:return json.dumps([{'Config':{'Labels':{runtime.LABEL:ID}}}]).encode()
                return b''
            self.assertEqual(runtime.cleanup(ID,SimpleNamespace(run=run),root=root),FLAGS)
            self.assertIn('--all',calls[0])
            self.assertTrue(any('rm' in c for c in calls));self.assertFalse((root/ID).exists())
    def test_foreign_label_never_removes_container_or_private_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/ID).mkdir();calls=[]
            def run(args,**kwargs):
                calls.append(args)
                return b'd'*12 if 'ls' in args else json.dumps([{'Config':{'Labels':{runtime.LABEL:'f'*32}}}]).encode()
            value=runtime.cleanup(ID,SimpleNamespace(run=run),root=root)
            self.assertFalse(value['containers_removed']);self.assertFalse(any('rm' in c for c in calls));self.assertTrue((root/ID).exists())
    def test_job_identifier_and_symlink_roots_cannot_reach_live_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);link=root/'link';link.symlink_to(root,target_is_directory=True)
            for identifier,target in [('../live',root),(ID,link),('a'*12,root)]:
                with self.subTest(identifier=identifier),self.assertRaises(BackupError):runtime.job_directory(identifier,root=target)
    def test_moodle_compose_is_fully_isolated_and_cannot_reuse_archived_commands(self):
        images=dict.fromkeys(('php','postgres','apache'),'sha256:'+'b'*64)
        value=moodle.compose_config('/private/test/'+ID,ID,images)
        self.assertTrue(value['networks']['isolated']['internal'])
        for name,service in value['services'].items():
            self.assertNotIn('ports',service);self.assertNotIn('privileged',service)
            self.assertEqual(service['labels'][runtime.LABEL],ID)
            self.assertEqual(service['image'],images[name]);self.assertEqual(service['restart'],'no')
            for volume in service['volumes']:self.assertTrue(volume['source'].startswith('/private/test/'+ID+'/'))
        self.assertNotIn('/opt/moodle',json.dumps(value));self.assertNotIn('docker.sock',json.dumps(value))


class ProofTests(unittest.TestCase):
    def test_success_requires_cleanup_and_database_proof_and_redacts_unknown_fields(self):
        self.assertNotIn('secret',result_document({**GOOD,'secret':'sftp-key','stdout':'secret'}))
        for value in [{**GOOD,'production_changed':True},{**GOOD,'cleanup':{**FLAGS,'networks_removed':False}},
                      {**GOOD,'database_check':None},{**GOOD,'rows_verified':True},{**GOOD,'reason':'secret /etc/file'}]:
            with self.subTest(value=value),self.assertRaises(ValueError):result_document(value)
    def test_failed_operation_always_cleans_and_cannot_claim_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/ID).mkdir();calls=[]
            def clean(identifier,**kwargs):calls.append(identifier);return FLAGS
            with patch.object(operation.moodle,'execute',side_effect=BackupError('checksum_mismatch')):
                value=operation.execute(CONFIG,JOB,root=root,commands=object(),clean=clean)
            self.assertEqual(value['status'],'failed');self.assertEqual(calls,[ID])
    def test_cleanup_failure_makes_an_otherwise_successful_test_inconclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/ID).mkdir()
            with patch.object(operation.moodle,'execute',return_value=dict(status='restore_validated',production_changed=False,database_check='passed')):
                value=operation.execute(CONFIG,JOB,root=root,commands=object(),clean=lambda *a,**k:{**FLAGS,'containers_removed':False})
            self.assertEqual(value['status'],'inconclusive')


class WorkerTests(unittest.TestCase):
    def test_success_uses_fixed_runner_project_and_minimal_job_descriptor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);runs=root/'runs';results=root/'results';artifacts=root/'runner'
            for directory in (runs,results,artifacts):directory.mkdir()
            calls=[]
            def runner(**kwargs):
                descriptor=json.loads((runs/ID/'job.json').read_text());calls.append(kwargs)
                self.assertEqual(set(descriptor),{'id','scope','snapshot_id','point'})
                private=Path(kwargs['private_data_dir'])
                temporary=private/'ansible-tmp'
                for key,name in (('ANSIBLE_HOME','home'),('ANSIBLE_LOCAL_TEMP','local'),('ANSIBLE_REMOTE_TEMP','remote')):
                    self.assertEqual(kwargs['envvars'][key],str(temporary/name))
                    self.assertTrue((temporary/name).is_dir())
                    self.assertEqual((temporary/name).stat().st_mode & 0o777,0o700)
                (temporary/'local/private-module').write_text('temporary data')
                (private/'audit-proof').write_text('audit retained')
                atomic_json(results/(ID+'.json'),GOOD)
                return SimpleNamespace(status='successful',rc=0)
            store=SimpleNamespace(finish=lambda *a,**k:(a,k))
            args,kwargs=worker.run_job(store,JOB,CONFIG,root=runs,results=results,runner_root=artifacts,runner=runner,clean=lambda *a,**k:FLAGS)
            self.assertEqual(args[2]['status'],'restore_validated');self.assertEqual(args[2]['runner_rc'],0)
            self.assertEqual(calls[0]['extravars'],{'recovery_test_job_id':ID})
            self.assertEqual(calls[0]['project_dir'],worker.PROJECT)
            self.assertNotIn(str(runs),calls[0]['private_data_dir'])
            self.assertFalse((artifacts/ID/'ansible-tmp').exists())
            self.assertTrue((artifacts/ID/'audit-proof').exists())
    def test_runner_zero_without_functional_report_is_not_restore_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name in ('runs','results','runner'):(root/name).mkdir()
            store=SimpleNamespace(finish=lambda *a,**k:a[2])
            result=worker.run_job(store,JOB,CONFIG,root=root/'runs',results=root/'results',runner_root=root/'runner',
                runner=lambda **kwargs:SimpleNamespace(status='successful',rc=0),clean=lambda *a,**k:FLAGS)
            self.assertEqual(result['status'],'inconclusive')
    def test_idle_worker_never_runs_a_test(self):
        store=SimpleNamespace(cleanup_pending=lambda:[],abandoned=lambda *a:[],claim=lambda *a:None)
        self.assertEqual(worker.process(store,CONFIG,ID,run=lambda *a:self.fail('dispatched'))['status'],'idle')
    def test_abandoned_execution_is_cleaned_without_replaying_it(self):
        calls=[];store=SimpleNamespace(cleanup_pending=lambda:[],abandoned=lambda *a:[JOB],finish=lambda *a,**k:calls.append((a,k)),claim=lambda *a:None)
        worker.process(store,CONFIG,ID,clean=lambda *a:FLAGS,run=lambda *a:self.fail('replayed'))
        self.assertTrue(calls[0][1]['interrupted']);self.assertEqual(calls[0][0][2]['status'],'inconclusive')

    def test_temporary_cleanup_failure_is_retried_without_replaying_the_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner=Path(tmp)/'runner';temporary=runner/ID/'ansible-tmp'
            temporary.mkdir(parents=True)
            (temporary/'module').write_text('private temporary data')
            audit=runner/ID/'audit-proof';audit.write_text('audit retained')
            observed=[]
            store=SimpleNamespace(cleanup_pending=lambda:[JOB],record_cleanup=lambda *a:observed.append(a[1]),
                abandoned=lambda *a:[],claim=lambda *a:None)
            with patch.object(worker,'RUNNER',runner):
                with patch.object(worker.shutil,'rmtree',side_effect=OSError('read-only')):
                    value=worker.process(store,CONFIG,ID,clean=lambda *a:FLAGS,run=lambda *a:self.fail('replayed'))
                self.assertEqual(value['status'],'cleanup_pending')
                self.assertFalse(observed[-1]['temporary_files_removed'])
                value=worker.process(store,CONFIG,ID,clean=lambda *a:FLAGS,run=lambda *a:self.fail('replayed'))
                self.assertEqual(value['status'],'idle')
                self.assertTrue(observed[-1]['temporary_files_removed'])
                self.assertFalse(temporary.exists());self.assertTrue(audit.exists())

    def test_symlinked_temporary_directory_never_removes_its_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);runner=root/'runner';private=runner/ID;private.mkdir(parents=True)
            outside=root/'outside';outside.mkdir();marker=outside/'retained';marker.write_text('retained')
            (private/'ansible-tmp').symlink_to(outside,target_is_directory=True)
            value=worker.clean_ansible_temporaries(ID,FLAGS,runner_root=runner)
            self.assertFalse(value['temporary_files_removed']);self.assertTrue(marker.exists())

    def test_real_ansible_runner_dispatches_fixed_playbook_and_reads_private_proof(self):
        import yaml
        import ansible_runner
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name in ('runs','results','runner','project'):(root/name).mkdir()
            script=root/'fixture.py'
            script.write_text('import json,sys,os;os.umask(0o077);from pathlib import Path;Path(sys.argv[1]).write_text(json.dumps('+repr(GOOD)+'))')
            project=root/'project'
            (project/'ansible.cfg').write_text('[defaults]\nretry_files_enabled=False\nlocalhost_warning=False\n')
            play=[{'hosts':'localhost','connection':'local','gather_facts':False,'tasks':[{'ansible.builtin.command':{'argv':[sys.executable,str(script),str(root/'results'/(ID+'.json'))]},'changed_when':False}]}]
            (project/'test-restore.yml').write_text(yaml.safe_dump(play))
            store=SimpleNamespace(finish=lambda *a,**k:a[2])
            # Le service systemd protege le dossier personnel en lecture seule.
            # Ansible doit donc ignorer ces emplacements herites non inscriptibles.
            with patch.object(worker,'PROJECT',str(project)),patch.object(worker,'PYTHON',sys.executable), \
                    patch.dict(os.environ,{'ANSIBLE_HOME':'/sys/moodle-runner-readonly-home',
                        'ANSIBLE_LOCAL_TEMP':'/sys/moodle-runner-readonly-local',
                        'ANSIBLE_REMOTE_TEMP':'/sys/moodle-runner-readonly-remote'}):
                proof=worker.run_job(store,JOB,CONFIG,root=root/'runs',results=root/'results',runner_root=root/'runner',
                    clean=lambda *a,**k:FLAGS)
            self.assertEqual(proof['status'],'restore_validated')
            self.assertEqual(proof['runner_status'],'successful');self.assertEqual(proof['runner_rc'],0)
            self.assertFalse((root/'runner'/ID/'ansible-tmp').exists())


if __name__=='__main__':unittest.main()
