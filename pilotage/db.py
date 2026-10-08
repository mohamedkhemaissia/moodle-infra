"""Connexions PostgreSQL bornees ; SQLite reste un format de transition explicite.

Le petit adaptateur garde les requetes et transitions existantes. Il n'accepte
que le SQL interne du projet ; les valeurs sont toujours liees par Psycopg.
Aucun repli SQLite n'a lieu si une configuration PostgreSQL est fournie.
"""
import json
import os
from pathlib import Path
import re
import sqlite3

from .schemas import APP_IDS, SCHEMAS


def load_config(path=None):
    path = path if path is not None else os.environ.get('PILOTAGE_DATABASE_CONFIG', '')
    if not path:
        return None
    try:
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError('duplicate')
                result[key] = value
            return result
        config_path = Path(path)
        if not config_path.is_absolute() or config_path.is_symlink():
            raise ValueError('path')
        data = config_path.read_bytes()
        if len(data) > 4096:
            raise ValueError('size')
        value = json.loads(data, object_pairs_hook=pairs)
        if not isinstance(value, dict) or set(value) != {'host', 'port', 'dbname', 'user', 'password_file'}:
            raise ValueError('fields')
        if value['host'] != '127.0.0.1' or type(value['port']) is not int or not 1024 <= value['port'] <= 65535:
            raise ValueError('address')
        for field in ('dbname', 'user'):
            if not isinstance(value[field], str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', value[field]):
                raise ValueError('identifier')
        password_file = Path(value.pop('password_file'))
        if not password_file.is_absolute() or password_file.is_symlink():
            raise ValueError('secret path')
        if password_file.stat().st_mode & 0o007:
            raise ValueError('secret permissions')
        password = password_file.read_text().strip()
        if not re.fullmatch(r'[a-f0-9]{64}', password):
            raise ValueError('secret format')
        return {**value, 'password': password}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise sqlite3.OperationalError('Configuration PostgreSQL absente ou invalide.') from None


def raw_connect(config, *, migration=False, read_only=False):
    try:
        import psycopg
        options = '-c statement_timeout={} -c lock_timeout=1500 -c idle_in_transaction_session_timeout=120000'.format(
            120000 if migration else 5000)
        if read_only:
            options += ' -c default_transaction_read_only=on'
        return psycopg.connect(**config, autocommit=True, connect_timeout=3,
                               application_name='moodle-pilotage', options=options)
    except Exception:
        # Jamais d'URL, de mot de passe ni de requete utilisateur dans les erreurs API.
        raise sqlite3.OperationalError('Connexion PostgreSQL indisponible.') from None


class Row:
    def __init__(self, names, values):
        self.names, self.values = tuple(names), tuple(values)
    def keys(self):
        return self.names
    def __getitem__(self, key):
        return self.values[key if isinstance(key, int) else self.names.index(key)]
    def __iter__(self):
        return iter(self.values)


class Cursor:
    def __init__(self, cursor, lastrowid=None):
        self.cursor, self.lastrowid = cursor, lastrowid
    def row(self, value):
        return None if value is None else Row([c.name for c in self.cursor.description], value)
    def fetchone(self):
        return self.row(self.cursor.fetchone())
    def fetchall(self):
        return [self.row(value) for value in self.cursor.fetchall()]


class Connection:
    """Une transaction par ecriture et des lectures coherentes en repeatable read."""
    def __init__(self, config, name, *, write=False):
        self.name, self.write, self.in_transaction = name, write, False
        self.raw = raw_connect(config, read_only=not write)
        try:
            self.raw.execute('SELECT set_config(%s,%s,false)', ('search_path', SCHEMAS[name] + ',pg_catalog'))
            self.check_schema()
        except Exception:
            self.raw.close()
            raise

    def check_schema(self):
        try:
            row = self.raw.execute('SELECT version,application_id FROM schema_info WHERE id=1').fetchone()
            if row != (1, APP_IDS[self.name]):
                raise sqlite3.DatabaseError('Version du stockage PostgreSQL incompatible.')
        except sqlite3.Error:
            raise
        except Exception:
            raise sqlite3.DatabaseError('Schema PostgreSQL absent ou incompatible.') from None

    def begin(self):
        if self.in_transaction:
            return
        self.raw.execute('BEGIN' if self.write else 'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        self.in_transaction = True
        if self.write:
            # Serialize les transitions et reservations entre processus/hotes.
            self.raw.execute('SELECT pg_advisory_xact_lock(%s,%s)', (APP_IDS[self.name], list(SCHEMAS).index(self.name)))

    def execute(self, statement, parameters=()):
        try:
            command = statement.strip().upper()
            if command in ('BEGIN', 'BEGIN IMMEDIATE'):
                self.begin()
                return self
            if command in ('COMMIT', 'ROLLBACK'):
                result = self.raw.execute(command)
                self.in_transaction = False
                return Cursor(result)
            # Ces requetes internes ne contiennent pas de '?' litteral.
            query = statement.replace('%', '%%').replace('?', '%s') if parameters else statement
            generated_id = bool(re.match(r'\s*INSERT INTO (incidents|events)\s*\(', statement, re.I))
            if generated_id:
                query += ' RETURNING id'
            cursor = self.raw.execute(query, parameters or None)
            lastrowid = cursor.fetchone()[0] if generated_id else None
            return Cursor(cursor, lastrowid)
        except sqlite3.Error:
            raise
        except Exception:
            raise sqlite3.DatabaseError('Operation PostgreSQL impossible.') from None

    def __enter__(self):
        try:
            self.begin()
            return self
        except Exception:
            self.close()
            raise sqlite3.OperationalError('Transaction PostgreSQL indisponible.') from None

    def __exit__(self, kind, value, traceback):
        try:
            if self.in_transaction:
                self.execute('ROLLBACK' if kind else 'COMMIT')
        finally:
            self.close()

    def close(self):
        self.raw.close()
