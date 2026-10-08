"""Verifier les vrais backends PostgreSQL 16 sur le service ephemere de CI."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

import psycopg
from psycopg import sql
from prepare_api import prepare


def main():
    root = Path(__file__).resolve().parents[1]
    password = os.environ['PG_TEST_ADMIN_PASSWORD']
    port = int(os.environ.get('PG_TEST_PORT', '15432'))
    # Base reservee aux tests, serveur local ephemere ; aucun acces au pilote.
    with psycopg.connect(host='127.0.0.1', port=port, dbname='moodle_pilotage', user='postgres',
                         password=password, connect_timeout=5, autocommit=True) as connection:
        version = connection.execute('SHOW server_version_num').fetchone()[0]
        if int(version) // 10000 != 16:
            raise RuntimeError('PostgreSQL 16 requis pour cette suite.')
        connection.execute(sql.SQL('CREATE ROLE pilotage LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                                   'NOREPLICATION NOBYPASSRLS PASSWORD {}').format(sql.Literal('a' * 64)))
        connection.execute('ALTER DATABASE moodle_pilotage OWNER TO pilotage')
        connection.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
    with tempfile.TemporaryDirectory(prefix='moodle-postgresql-tests-') as directory:
        temporary = Path(directory)
        context = temporary / 'api'
        prepare(root, context)
        secret = temporary / 'password'
        secret.write_text('a' * 64 + '\n')
        secret.chmod(0o600)
        config = temporary / 'database.json'
        config.write_text(json.dumps({'host': '127.0.0.1', 'port': port, 'dbname': 'moodle_pilotage',
                                      'user': 'pilotage', 'password_file': str(secret)}))
        os.environ['TEST_POSTGRES_CONFIG'] = str(config)
        sys.path[:0] = [str(context), str(root)]
        suite = unittest.TestLoader().discover(str(root / 'tests/postgresql'), pattern='test_*.py')
        if suite.countTestCases() == 0:
            raise RuntimeError('Suite PostgreSQL absente.')
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if result.skipped:
            raise RuntimeError('Aucun test PostgreSQL ne doit etre saute.')
        return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
