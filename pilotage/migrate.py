"""Copie verifiee SQLite -> PostgreSQL, sans suppression des journaux sources.

L'observateur doit etre arrete avant la bascule. Les verrous des trois commandes
sont conserves jusqu'a la publication du marqueur d'activation, apres COMMIT.
"""
import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile

from .db import load_config, raw_connect
from .schemas import APP_IDS, DDL, SCHEMAS, TABLES


class MigrationError(RuntimeError):
    pass


@contextmanager
def source_snapshot(directories):
    if set(directories) != set(TABLES):
        raise MigrationError('Les trois journaux sources sont requis.')
    filenames = {'incidents': 'incidents.sqlite3', 'diagnostics': 'diagnostics.sqlite3',
                 'remediation': 'remediations.sqlite3'}
    with ExitStack() as stack:
        result = {}
        for name, tables in TABLES.items():
            directory = Path(directories[name]).resolve()
            lock_file = directory / ('observer.lock' if name == 'incidents' else 'worker.lock')
            if lock_file.is_symlink():
                raise MigrationError('Chemin du verrou invalide.')
            existed = lock_file.exists()
            descriptor = os.open(lock_file, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            lock = stack.enter_context(os.fdopen(descriptor, 'a+b'))
            if not existed:
                # L'observateur non root doit pouvoir reutiliser le verrou apres bascule.
                owner = directory.stat()
                if (os.fstat(descriptor).st_uid, os.fstat(descriptor).st_gid) != (owner.st_uid, owner.st_gid):
                    os.fchown(descriptor, owner.st_uid, owner.st_gid)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MigrationError('Un observateur, diagnostic ou correctif est encore actif.') from None
            source = directory / filenames[name]
            if source.is_symlink() or not source.is_file():
                raise MigrationError('Journal source absent ou lien symbolique.')
            connection = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=1.5)
            stack.callback(connection.close)
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            if connection.execute('PRAGMA application_id').fetchone()[0] != APP_IDS[name] or \
                    connection.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise MigrationError('Version SQLite incompatible.')
            if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or \
                    connection.execute('PRAGMA foreign_key_check').fetchall():
                raise MigrationError('Journal SQLite incoherent.')
            if name != 'incidents' and connection.execute("SELECT 1 FROM runs WHERE status='running' LIMIT 1").fetchone():
                raise MigrationError('Execution encore active ou interrompue : la traiter avant migration.')
            result[name] = {}
            for table in tables:
                # Noms issus uniquement de TABLES ; jamais de parametre utilisateur SQL.
                info = connection.execute('PRAGMA table_info(' + table + ')').fetchall()
                if not info:
                    raise MigrationError('Table source absente.')
                columns = [column[1] for column in info]
                primary = [column[1] for column in sorted(info, key=lambda c: c[5]) if column[5]]
                rows = connection.execute('SELECT * FROM ' + table + ' ORDER BY ' + ','.join(primary)).fetchall()
                for row in rows:
                    for value in row:
                        if isinstance(value, float) and not math.isfinite(value):
                            raise MigrationError('Nombre non fini dans le journal source.')
                sequence = 0
                if name == 'incidents' and table in ('incidents', 'events'):
                    saved = connection.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (table,)).fetchone()
                    sequence = saved[0] if saved is not None else 0
                result[name][table] = {'columns': columns, 'primary': primary, 'rows': rows,
                                      'sequence': sequence}
        yield result


def digest(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                     separators=(',', ':')).encode()).hexdigest()


def activate(path, fingerprint):
    """Publication atomique du marqueur, sans ecraser un marqueur different."""
    path = Path(path)
    payload = json.dumps({'version': 1, 'source_sha256': fingerprint}, sort_keys=True) + '\n'
    if path.is_symlink():
        raise MigrationError('Marqueur d activation invalide.')
    if path.exists():
        if path.read_text() != payload:
            raise MigrationError('Marqueur d activation incompatible.')
        return
    descriptor, temporary = tempfile.mkstemp(prefix='.activation-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def migrate(config_path, directories, *, activation_file=None):
    import psycopg
    from psycopg import sql
    config = load_config(config_path)
    if config is None:
        raise MigrationError('Une configuration PostgreSQL explicite est requise.')
    with source_snapshot(directories) as snapshot:
        fingerprint = digest(snapshot)
        counts = {name: {table: len(data['rows']) for table, data in tables.items()}
                  for name, tables in snapshot.items()}
        connection = raw_connect(config, migration=True)
        try:
            with connection.transaction():
                # Verrou global de migration entre operateurs/hotes.
                connection.execute('SELECT pg_advisory_xact_lock(%s,%s)', (APP_IDS['incidents'], 100))
                exists = connection.execute("SELECT to_regclass('pilotage_incidents.migration_info')").fetchone()[0]
                if exists:
                    prior = connection.execute('SELECT source_sha256 FROM pilotage_incidents.migration_info WHERE id=1').fetchone()
                    if prior != (fingerprint,):
                        raise MigrationError('La cible contient une migration differente ; aucune donnee ecrasee.')
                    for name in TABLES:
                        version = connection.execute(sql.SQL('SELECT version,application_id FROM {}.schema_info WHERE id=1')
                                                     .format(sql.Identifier(SCHEMAS[name]))).fetchone()
                        if version != (1, APP_IDS[name]):
                            raise MigrationError('Version du schema cible incompatible.')
                    status = 'already_migrated'
                else:
                    for schema in SCHEMAS.values():
                        if connection.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (schema,)).fetchone():
                            raise MigrationError('Schema cible deja present ; aucune table ecrasee.')
                    for name, tables in snapshot.items():
                        schema = SCHEMAS[name]
                        connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
                        connection.execute('SELECT set_config(%s,%s,true)', ('search_path', schema + ',pg_catalog'))
                        connection.execute('CREATE TABLE schema_info (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, application_id BIGINT NOT NULL)')
                        connection.execute('INSERT INTO schema_info VALUES(1,1,%s)', (APP_IDS[name],))
                        for statement in DDL[name]:
                            connection.execute(statement)
                        for table, data in tables.items():
                            target_columns = [c[0] for c in connection.execute(sql.SQL('SELECT * FROM {} LIMIT 0').format(sql.Identifier(table))).description]
                            if data['columns'] != target_columns:
                                raise MigrationError('Colonnes source et cible incompatibles.')
                            insert = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
                                sql.Identifier(table), sql.SQL(',').join(map(sql.Identifier, data['columns'])),
                                sql.SQL(',').join(sql.Placeholder() for _ in data['columns']))
                            with connection.cursor() as cursor:
                                cursor.executemany(insert, data['rows'])
                            copied = connection.execute(sql.SQL('SELECT * FROM {} ORDER BY {}').format(
                                sql.Identifier(table), sql.SQL(',').join(map(sql.Identifier, data['primary'])))).fetchall()
                            if digest(copied) != digest(data['rows']):
                                raise MigrationError('Verification des donnees copiees en echec.')
                    # Conserver aussi les IDs attribues puis supprimes par SQLite.
                    connection.execute("SELECT set_config('search_path','pilotage_incidents,pg_catalog',true)")
                    for table in ('incidents', 'events'):
                        maximum = max(snapshot['incidents'][table]['sequence'],
                                      max((row[0] for row in snapshot['incidents'][table]['rows']), default=0))
                        sequence = connection.execute('SELECT pg_get_serial_sequence(%s,%s)', (table, 'id')).fetchone()[0]
                        connection.execute('SELECT setval(%s,%s,%s)', (sequence, max(1, maximum), maximum > 0))
                    connection.execute('CREATE TABLE migration_info (id INTEGER PRIMARY KEY CHECK(id=1),source_sha256 TEXT NOT NULL,counts_json TEXT NOT NULL)')
                    connection.execute('INSERT INTO migration_info VALUES(1,%s,%s)', (fingerprint, json.dumps(counts, sort_keys=True)))
                    status = 'migrated'
            # COMMIT precede l'activation ; sources verrouillees jusqu'ici.
            if activation_file is not None:
                activate(activation_file, fingerprint)
            return {'status': status, 'source_sha256': fingerprint, 'verified_rows': counts,
                    'source_files_preserved': True, 'activated': activation_file is not None}
        except (MigrationError, sqlite3.Error):
            raise
        except psycopg.Error:
            raise MigrationError('Migration PostgreSQL interrompue ; transaction annulee.') from None
        finally:
            connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    for name in TABLES:
        parser.add_argument('--' + name + '-dir', required=True)
    parser.add_argument('--activation-file')
    args = parser.parse_args()
    try:
        report = migrate(args.config, {name: getattr(args, name + '_dir') for name in TABLES},
                         activation_file=args.activation_file)
    except MigrationError as exc:
        parser.exit(1, 'Migration arretee : ' + str(exc) + ' Les sources sont conservees.\n')
    except (OSError, sqlite3.Error, ValueError):
        parser.exit(1, 'Migration arretee : journaux, verrous ou cible incompatibles. Les sources sont conservees.\n')
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
