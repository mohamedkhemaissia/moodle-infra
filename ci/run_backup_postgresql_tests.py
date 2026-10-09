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
from pilotage.schemas import DDL, SCHEMAS, APP_IDS
from pilotage_backup.core import DatabaseSnapshot
from pilotage_backup.inventory import inventory_sql, identity_check_sql, validate_inventory, TABLES


def main():
    admin_password=os.environ['PG_TEST_ADMIN_PASSWORD']
    port=int(os.environ.get('PG_TEST_PORT','15432'))
    # Un role unique et deux bases de CI : aucune suppression du service pilote.
    suffix=uuid.uuid4().hex[:12]
    role='backup_ci_'+suffix
    source='backup_source_'+suffix;target='backup_restored_'+suffix
    password='b'*64
    admin=psycopg.connect(host='127.0.0.1',port=port,dbname='postgres',user='postgres',
                         password=admin_password,autocommit=True,connect_timeout=5)
    created=[];role_created=False
    try:
        admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION '
            'NOBYPASSRLS PASSWORD {}').format(sql.Identifier(role),sql.Literal(password)));role_created=True
        for name in (source,target):
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
        with tempfile.TemporaryDirectory(prefix='pilotage-backup-pg-ci-') as temporary:
            archive=Path(temporary)/'pilotage.pgdump'
            with DatabaseSnapshot(config) as capture:
                snapshot=capture.snapshot_id
                expected=capture.inventory
                # Une ecriture apres l'instantane ne doit pas entrer dans le dump.
                with psycopg.connect(**config,autocommit=True) as writer:
                    writer.execute("INSERT INTO pilotage_incidents.metadata VALUES('after_snapshot','must not be copied')")
                subprocess.run(['/usr/lib/postgresql/16/bin/pg_dump','-h','127.0.0.1','-p',str(port),
                    '-U',role,'-d',source,'--snapshot='+snapshot,'--format=custom','--no-acl','-f',str(archive)],
                    env=env,check=True,timeout=120,capture_output=True)
            subprocess.run(['/usr/lib/postgresql/16/bin/pg_restore','-h','127.0.0.1','-p',str(port),
                '-U',role,'-d',target,'--no-owner','--no-acl','--single-transaction','--exit-on-error',str(archive)],
                env=env,check=True,timeout=120,capture_output=True)
            with psycopg.connect(**{**config,'dbname':target},autocommit=True) as connection:
                actual=validate_inventory(connection.execute(inventory_sql()).fetchone()[0],database=target)
                actual['database']=source
                if actual!=expected:raise RuntimeError('La copie ne correspond pas a l instantane exporte.')
                if connection.execute('SELECT source_sha256,counts_json FROM '
                        'pilotage_incidents.migration_info WHERE id=1').fetchone()!=('a'*64,'{}'):
                    raise RuntimeError('La preuve de migration n a pas ete restauree.')
                if not all(v is True for v in connection.execute(identity_check_sql()).fetchone()[0].values()):
                    raise RuntimeError('Identites restaurees incorrectes.')
                if connection.execute("SELECT count(*) FROM pilotage_incidents.metadata WHERE key='after_snapshot'").fetchone()[0]!=0:
                    raise RuntimeError('Une ecriture future a fuit dans le dump.')
                connection.execute("INSERT INTO pilotage_auth.users(username,display_name,role,password_hash,created_at,updated_at) "
                                   "VALUES('next','Compte suivant','reader','fixture',2,2)")
                if connection.execute("SELECT id FROM pilotage_auth.users WHERE username='next'").fetchone()[0]!=2:
                    raise RuntimeError('La sequence des comptes a ete perdue.')
            print(json.dumps({'status':'passed','scope':'real_postgresql_16_ci_fixture',
                'tables_verified':len(TABLES),'consistent_exported_snapshot':True,'pg_dump_restore':True,
                'sequences_preserved':True,'migration_proof_preserved':True,'production_data_used':False},indent=2))
    finally:
        for name in reversed(created):
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        if role_created:admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
        admin.close()


if __name__=='__main__':main()
