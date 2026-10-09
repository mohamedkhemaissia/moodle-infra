"""Vrais conteneurs PostgreSQL/PHP/Apache ; donnees Moodle fictives, aucun pilote."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
from unittest.mock import patch
import uuid

import yaml
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from recovery_tests import archives, moodle, operation, runtime
from pilotage_backup.core import BackupError

ROOT=Path(__file__).resolve().parents[1]


def run(*args,input=None):
    try:return subprocess.run(args,input=input,capture_output=True,check=True,timeout=300).stdout
    except subprocess.CalledProcessError as exc:
        # En CI les sorties ne contiennent que les fixtures, pas les sauvegardes d'entreprise.
        raise RuntimeError('Commande fixture en echec: '+str(args[:3])+': '+exc.stderr.decode(errors='replace')[-2500:]) from None


def main():
    if os.geteuid()!=0:raise RuntimeError('Executer uniquement sous sudo dans le job de CI ephemere.')
    identifier=uuid.uuid4().hex;seed='moodle-restore-ci-seed-'+identifier
    defaults=yaml.safe_load((ROOT/'ansible/roles/moodle/defaults/main.yml').read_text())
    pg=defaults['moodle_postgres_image'];httpd=defaults['moodle_apache_image']
    tag='moodle-pfe/php:moodle-5.2.3-'+identifier[:12]
    with tempfile.TemporaryDirectory(prefix='moodle-restore-native-ci-') as tmp:
        root=Path(tmp);payload=root/'payload';payload.mkdir();build=root/'build';build.mkdir()
        jobid=uuid.uuid4().hex;runs=root/'runs';runs.mkdir();directory=runs/jobid;directory.mkdir(mode=0o700)
        (build/'Dockerfile').write_text('FROM '+defaults['moodle_php_base_image']+'\nRUN apt-get update && apt-get install -y --no-install-recommends libpq-dev && docker-php-ext-install pgsql && rm -rf /var/lib/apt/lists/*\n')
        try:
            run('/usr/bin/docker','pull',pg);run('/usr/bin/docker','pull',httpd)
            run('/usr/bin/docker','build','--tag',tag,str(build))
            run('/usr/bin/docker','run','--detach','--name',seed,'--network','none','--env','POSTGRES_HOST_AUTH_METHOD=trust','--env','POSTGRES_DB=moodle',pg)
            deadline=time.monotonic()+90
            while True:
                try:run('/usr/bin/docker','exec',seed,'pg_isready','-h','127.0.0.1','-U','postgres','-d','moodle');break
                except RuntimeError:
                    if time.monotonic()>=deadline:raise
                    time.sleep(1)
            sql=b"CREATE TABLE mdl_course(id integer);INSERT INTO mdl_course VALUES(1);CREATE TABLE mdl_user(id integer);INSERT INTO mdl_user VALUES(1);CREATE TABLE mdl_quiz(id integer);INSERT INTO mdl_quiz VALUES(1);CREATE TABLE mdl_config(name text);INSERT INTO mdl_config VALUES('version');"
            run('/usr/bin/docker','exec','-i',seed,'psql','-X','-U','postgres','-d','moodle','-v','ON_ERROR_STOP=1',input=sql)
            (payload/'moodle.pgdump').write_bytes(run('/usr/bin/docker','exec',seed,'pg_dump','-U','postgres','-d','moodle','--format=custom'))
            code=root/'code';data=root/'data';configuration=root/'configuration';infrastructure=root/'infrastructure'
            for path in (code/'vendor',code/'public/login',code/'admin/cli',data,configuration/'secrets',infrastructure):path.mkdir(parents=True,exist_ok=True)
            (code/'vendor/autoload.php').write_text('<?php')
            (code/'public/version.php').write_text("<?php $release='5.2.3';")
            password='Pfe!1-'+'a'*64
            (configuration/'secrets/moodle_db_password').write_text(password)
            (configuration/'compose.yml').write_text(json.dumps({'services':{'postgres':{'image':pg},'apache':{'image':httpd},'php':{'image':tag}}}))
            (code/'config.php').write_text('''<?php
$CFG=new stdClass();$CFG->wwwroot='https://moodle-pfe.test:8443';$CFG->dataroot='/var/moodledata';
$DB=new class {
 function connection(){return pg_connect("host=postgres dbname=moodle user=moodle password=".trim(file_get_contents('/run/secrets/moodle_db_password')));}
 function record_exists($table,$conditions){return pg_num_rows(pg_query($this->connection(),"SELECT * FROM mdl_config WHERE name='version'"))===1;}
 function count_records($table){return (int)pg_fetch_result(pg_query($this->connection(),'SELECT count(*) FROM mdl_'.$table),0,0);}
};
''')
            (code/'public/login/index.php').write_text('''<?php require(dirname(__DIR__,2).'/config.php');if(!$DB->record_exists('config',[])){http_response_code(500);exit();} ?><form><input name="username"><input name="password" type="password"></form>''')
            (code/'admin/cli/purge_caches.php').write_text('<?php echo "fixture caches ready";')
            (data/'persistent-file.txt').write_text('copie fictive')
            for name,source in [('code.tar',code),('moodledata.tar',data),('configuration.tar',configuration),('infrastructure.tar',infrastructure)]:
                with tarfile.open(payload/name,'w') as archive:archive.add(source,arcname='.')
            (payload/'manifest.txt').write_text('Format sauvegarde : moodle-pfe-v1\nInstant sans ecritures UTC : 2026-10-09T10:59:00Z\nProjet Compose : moodle-pilote\nBase : moodle\nMoodle : 5.2.3\nURL source : https://moodle-pfe.test:8443\n')
            checks=''.join(hashlib.sha256((payload/name).read_bytes()).hexdigest()+'  '+name+'\n' for name in archives.ARCHIVES)
            job=dict(id=jobid,scope='moodle',snapshot_id='b'*64,point=dict(id='b'*64,archived_at='2026-10-09T11:00:00Z',hostname='ci'))
            class FixtureCommands(runtime.Commands):
                def run(self,args,**kwargs):
                    if args[0].endswith('-restic'):
                        if 'snapshots' in args:return json.dumps([dict(id='b'*64,time='2026-10-09T11:00:00Z',hostname='ci',tags=['moodle-pilote-full'],paths=['/opt/moodle/backups/staging/run-ci'])]).encode()
                        name=args[-1].split('/')[-1];value=checks.encode() if name=='SHA256SUMS' else (payload/name).read_bytes()
                        if kwargs.get('output') is not None:kwargs['output'].write(value);return b''
                        return value
                    return super().run(args,**kwargs)
            config=dict(moodle_project='moodle-pilote',moodle_base='/opt/moodle',run_seconds=600,min_free_mb=512)
            proof=operation.execute(config,job,root=runs,commands=FixtureCommands(600))
            if proof.get('status')!='restore_validated' or proof.get('login_check')!='passed' or not all(proof['cleanup'].values()):
                raise RuntimeError('Verification native de la fixture invalide: '+json.dumps(proof))
            if (proof['courses'],proof['users'],proof['quizzes'])!=(1,1,1):raise RuntimeError('Comptages fixture incorrects.')
            print(json.dumps({'status':'passed','scope':'real_docker_postgresql_php_apache_with_synthetic_moodle_files',
                'https_validated':True,'cleanup_verified':True,'production_data_used':False}))
        finally:
            flags=runtime.cleanup(jobid,root=runs)
            run('/usr/bin/docker','rm','--force',seed)
            if not all(flags.values()):raise RuntimeError('Nettoyage des ressources fixture incomplet.')


if __name__=='__main__':main()
