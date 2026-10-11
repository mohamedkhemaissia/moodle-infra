"""Verifier l'instantane exporte, pg_dump 16 et pg_restore avec des donnees de CI."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid
import sys

import psycopg
from psycopg import sql

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pilotage.auth import DDL as AUTH_DDL, APPLICATION_ID
from pilotage.recovery import DDL as RECOVERY_DDL, APPLICATION_ID as RECOVERY_ID
from pilotage.recovery_execution import DDL as EXECUTION_DDL
from pilotage.operating_policy import DDL as POLICY_DDL, APPLICATION_ID as POLICY_ID, digest
from pilotage.schemas import DDL, SCHEMAS, APP_IDS
from pilotage_backup.core import DatabaseSnapshot
from pilotage_backup.inventory import inventory_sql, identity_check_sql, validate_inventory, TABLES
from prepare_api import prepare


def main():
    admin_password=os.environ['PG_TEST_ADMIN_PASSWORD']
    port=int(os.environ.get('PG_TEST_PORT','15432'))
    # Un role unique et deux bases de CI : aucune suppression du service pilote.
    suffix=uuid.uuid4().hex[:12]
    role='backup_ci_'+suffix
    source='backup_source_'+suffix
    targets=('backup_restored_'+suffix, 'backup_recovery_'+suffix, 'backup_execution_'+suffix, 'backup_policy_'+suffix, 'backup_decisions_'+suffix)
    password='b'*64
    admin=psycopg.connect(host='127.0.0.1',port=port,dbname='postgres',user='postgres',
                         password=admin_password,autocommit=True,connect_timeout=5)
    created=[];role_created=False
    try:
        admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION '
            'NOBYPASSRLS PASSWORD {}').format(sql.Identifier(role),sql.Literal(password)));role_created=True
        for name in (source,*targets):
            admin.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(sql.Identifier(name),sql.Identifier(role)))
            created.append(name)
        config={'host':'127.0.0.1','port':port,'dbname':source,'user':role,'password':password}
        with psycopg.connect(**config,autocommit=True) as connection:
            for name in DDL:
                connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(SCHEMAS[name])))
                connection.execute(sql.SQL('SET search_path TO {},pg_catalog').format(sql.Identifier(SCHEMAS[name])))
                connection.execute('CREATE TABLE schema_info(id INTEGER PRIMARY KEY,version INTEGER,application_id INTEGER)')
                for statement in DDL[name]:connection.execute(statement)
                connection.execute('INSERT INTO schema_info VALUES(1,1,%s)',(APP_IDS[name],))
            # Table ajoutee par pilotage.migrate, en plus du DDL des journaux.
            connection.execute('CREATE TABLE pilotage_incidents.migration_info '
                '(id INTEGER PRIMARY KEY CHECK(id=1),source_sha256 TEXT NOT NULL,counts_json TEXT NOT NULL)')
            connection.execute('INSERT INTO pilotage_incidents.migration_info VALUES(1,%s,%s)',('a'*64,'{}'))
            connection.execute('CREATE SCHEMA pilotage_auth');connection.execute('SET search_path TO pilotage_auth,pg_catalog')
            for statement in AUTH_DDL:connection.execute(statement)
            connection.execute('INSERT INTO schema_info VALUES(1,1,%s)',(APPLICATION_ID,))
            connection.execute("INSERT INTO pilotage_incidents.metadata VALUES('preuve','texte avec é et retour\nligne')")
            connection.execute("INSERT INTO pilotage_auth.users(username,display_name,role,password_hash,created_at,updated_at) "
                               "VALUES('test','Compte CI','admin','hash-fixture-only',1,1)")
            connection.execute("INSERT INTO pilotage_auth.events(actor_id,target_id,action,observed_at) VALUES(1,1,'created',1)")
        env={**os.environ,'PGPASSWORD':password}
        for profile,target in zip(range(5),targets):
            include_recovery=profile>=1;include_execution=profile>=2;include_policy=profile>=3;include_decisions=profile>=4
            if profile==1:
                with psycopg.connect(**config,autocommit=True) as connection:
                    connection.execute("DELETE FROM pilotage_incidents.metadata WHERE key='after_snapshot'")
                    connection.execute('CREATE SCHEMA pilotage_recovery')
                    connection.execute('SET search_path TO pilotage_recovery,pg_catalog')
                    for statement in RECOVERY_DDL: connection.execute(statement)
                    connection.execute('INSERT INTO schema_info VALUES(1,1,%s)',(RECOVERY_ID,))
                    connection.execute("""INSERT INTO requests(id,scope,snapshot_id,target,reason,actor_id,actor_name,
                        client_key,payload_sha256,status,revision,created_at,updated_at,selected_json)
                        VALUES(%s,'pilotage',%s,'isolated_test','Verifier la copie isolee',1,'test',%s,%s,'pending',1,1,1,'{}')""",
                        ('a'*32,'d'*64,'b'*32,'e'*64))
                    connection.execute("INSERT INTO events(request_id,actor_id,actor_name,action,observed_at,revision,evidence_json) VALUES(%s,1,'test','created',1,1,'{}')", ('a'*32,))
            if profile==2:
                with psycopg.connect(**config,autocommit=True) as connection:
                    connection.execute("DELETE FROM pilotage_incidents.metadata WHERE key='after_snapshot'")
                    connection.execute('SET search_path TO pilotage_recovery,pg_catalog')
                    for statement in EXECUTION_DDL:connection.execute(statement)
                    connection.execute("""INSERT INTO executions(id,request_id,scope,snapshot_id,status,actor_id,actor_name,credential_json,approver_version,queued_at,updated_at)
                        VALUES(%s,%s,'pilotage',%s,'queued',1,'test','{}',1,1,1)""",('c'*32,'a'*32,'d'*64))
                    connection.execute("INSERT INTO execution_events(execution_id,actor_id,actor_name,action,observed_at,revision,evidence_json) VALUES(%s,1,'test','queued',1,1,'{}')",('c'*32,))
            if profile==3:
                with psycopg.connect(**config,autocommit=True) as connection:
                    connection.execute("DELETE FROM pilotage_incidents.metadata WHERE key='after_snapshot'")
                    connection.execute('CREATE SCHEMA pilotage_policy')
                    connection.execute('SET search_path TO pilotage_policy,pg_catalog')
                    for statement in POLICY_DDL:connection.execute(statement)
                    connection.execute('INSERT INTO schema_info VALUES(1,1,%s)',(POLICY_ID,))
                    policy={'revision':1,'mode':'observation','suspended':False,'evaluation_context':False}
                    connection.execute("INSERT INTO settings VALUES(1,1,'observation',false,false,1,1,'test','Fixture de sauvegarde',%s)",(digest(policy),))
                    connection.execute("INSERT INTO events(actor_id,actor_name,action,observed_at,revision,details_json) VALUES(1,'test','policy_updated',1,1,'{}')")
            if include_decisions:
                with tempfile.TemporaryDirectory(prefix='pilotage-decision-backup-') as directory:
                    temporary = Path(directory)
                    context = temporary / 'api'
                    prepare(Path(__file__).resolve().parents[1], context)
                    sys.path.insert(0, str(context))
                    from app.incidents import IncidentStore, Observation, DEFINITIONS
                    secret = temporary / 'password'
                    secret.write_text(password); secret.chmod(0o600)
                    config_path = temporary / 'database.json'
                    config_path.write_text(json.dumps({key: value for key, value in config.items() if key != 'password'} |
                        {'password_file': str(secret)}))
                    observer = IncidentStore(temporary, database_config=config_path, decision_journal_enabled=True)
                    observer.initialize()
                    for point in (1700000010,1700000040):
                        observer.apply(point, [Observation(key, 'triggered' if key=='cpu_high' else 'clear',
                            {'source':'CI sauvegarde du journal'}) for key in DEFINITIONS], completed_at=point+1)
                    if len(observer.decision_journal.list(1700000042)['items'])!=1:
                        raise RuntimeError('La fixture doit contenir une decision reellement evaluee.')
                with psycopg.connect(**config,autocommit=True) as connection:
                    connection.execute("DELETE FROM pilotage_incidents.metadata WHERE key='after_snapshot'")
            with tempfile.TemporaryDirectory(prefix='pilotage-backup-pg-ci-') as temporary:
                archive=Path(temporary)/('pilotage-recovery.pgdump' if include_recovery else 'pilotage.pgdump')
                with DatabaseSnapshot(config) as capture:
                    snapshot=capture.snapshot_id
                    expected=capture.inventory
                    # Une ecriture apres l'instantane ne doit pas entrer dans le dump.
                    with psycopg.connect(**config,autocommit=True) as writer:
                        writer.execute("INSERT INTO pilotage_incidents.metadata VALUES('after_snapshot','must not be copied') ON CONFLICT(key) DO NOTHING")
                    subprocess.run(['/usr/lib/postgresql/16/bin/pg_dump','-h','127.0.0.1','-p',str(port),
                        '-U',role,'-d',source,'--snapshot='+snapshot,'--format=custom','--no-acl','-f',str(archive)],
                        env=env,check=True,timeout=120,capture_output=True)
                subprocess.run(['/usr/lib/postgresql/16/bin/pg_restore','-h','127.0.0.1','-p',str(port),
                    '-U',role,'-d',target,'--no-owner','--no-acl','--single-transaction','--exit-on-error',str(archive)],
                    env=env,check=True,timeout=120,capture_output=True)
                with psycopg.connect(**{**config,'dbname':target},autocommit=True) as connection:
                    actual=validate_inventory(connection.execute(inventory_sql(include_recovery,include_execution,include_policy,include_decisions)).fetchone()[0],database=target)
                    actual['database']=source
                    if actual!=expected:raise RuntimeError('La copie ne correspond pas a l instantane exporte.')
                    if connection.execute('SELECT source_sha256,counts_json FROM '
                            'pilotage_incidents.migration_info WHERE id=1').fetchone()!=('a'*64,'{}'):
                        raise RuntimeError('La preuve de migration n a pas ete restauree.')
                    if not all(v is True for v in connection.execute(identity_check_sql(include_recovery,include_execution,include_policy,include_decisions)).fetchone()[0].values()):
                        raise RuntimeError('Identites restaurees incorrectes.')
                    if connection.execute("SELECT count(*) FROM pilotage_incidents.metadata WHERE key='after_snapshot'").fetchone()[0]!=0:
                        raise RuntimeError('Une ecriture future a fuit dans le dump.')
                    connection.execute("INSERT INTO pilotage_auth.users(username,display_name,role,password_hash,created_at,updated_at) "
                                       "VALUES('next','Compte suivant','reader','fixture',2,2)")
                    if connection.execute("SELECT id FROM pilotage_auth.users WHERE username='next'").fetchone()[0]!=2:
                        raise RuntimeError('La sequence des comptes a ete perdue.')
                    if include_recovery:
                        connection.execute("INSERT INTO pilotage_recovery.events(request_id,actor_id,actor_name,action,observed_at,revision,evidence_json) VALUES(%s,1,'test','approved',2,2,'{}')", ('a'*32,))
                        if connection.execute('SELECT max(id) FROM pilotage_recovery.events').fetchone()[0]!=2:
                            raise RuntimeError('La sequence des decisions de restauration a ete perdue.')
                    if include_execution:
                        connection.execute("INSERT INTO pilotage_recovery.execution_events(execution_id,actor_name,action,observed_at,revision,evidence_json) VALUES(%s,'worker','started',2,2,'{}')",('c'*32,))
                        if connection.execute('SELECT max(id) FROM pilotage_recovery.execution_events').fetchone()[0]!=2:
                            raise RuntimeError('La sequence des executions a ete perdue.')
                    if include_policy:
                        connection.execute("INSERT INTO pilotage_policy.events(actor_name,action,observed_at,revision,details_json) VALUES('test','policy_updated',2,2,'{}')")
                        if connection.execute('SELECT max(id) FROM pilotage_policy.events').fetchone()[0]!=2:
                            raise RuntimeError('La sequence des politiques a ete perdue.')
                    if include_decisions:
                        row = connection.execute('SELECT id,incident_id,signature,status,first_observed_at,last_observed_at,observations,decision_json,policy_json,collector_json FROM pilotage_decisions.entries').fetchone()
                        connection.execute('INSERT INTO pilotage_decisions.entries(incident_id,signature,status,first_observed_at,last_observed_at,observations,decision_json,policy_json,collector_json) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',row[1:])
                        if connection.execute('SELECT max(id) FROM pilotage_decisions.entries').fetchone()[0]!=row[0]+1:
                            raise RuntimeError('La sequence du journal des decisions a ete perdue.')
                print(json.dumps({'status':'passed' ,'scope':'real_postgresql_16_ci_fixture',
                    'tables_verified':len(expected['tables']), 'recovery_journal_included':include_recovery,'operating_policy_included':include_policy,'decision_journal_included':include_decisions,'consistent_exported_snapshot':True,'pg_dump_restore':True,
                    'sequences_preserved':True,'migration_proof_preserved':True,'production_data_used':False},indent=2))
    finally:
        for name in reversed(created):
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        if role_created:admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
        admin.close()


if __name__=='__main__':main()
