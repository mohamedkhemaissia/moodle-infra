"""Inventaire reproductible des schemas ; aucune ligne sensible dans les rapports."""
import hashlib
import json
import re

SCHEMAS = {
    # migrate() conserve egalement la preuve de copie SQLite -> PostgreSQL.
    'pilotage_incidents': (0x4D4F4F49, ('schema_info', 'metadata', 'signal_state', 'incidents', 'events', 'migration_info')),
    'pilotage_diagnostics': (0x4D444941, ('schema_info', 'runs')),
    'pilotage_remediation': (0x4D444941, ('schema_info', 'runs', 'claims')),
    'pilotage_auth': (0x4D4F4155, ('schema_info', 'users', 'sessions', 'login_limits', 'events')),
}
TABLES = tuple(f'{schema}.{table}' for schema, (_, tables) in SCHEMAS.items() for table in tables)
IDENTITIES = ('pilotage_incidents.incidents', 'pilotage_incidents.events',
              'pilotage_auth.users', 'pilotage_auth.events')


def inventory_sql():
    """Le meme SQL sur l'instantane source et sur la copie PostgreSQL 16.

    Les empreintes portent sur les lignes JSON ordonnees en collation C. Les
    comptes, hash de mots de passe, preuves et sessions ne sortent pas en clair.
    Cette premiere version borne les tables a celles du schema applicatif v1.
    """
    schema_names = ','.join("'" + name + "'" for name in SCHEMAS)
    versions = ','.join(
        "'" + schema + "', (SELECT json_build_array(version,application_id) FROM "
        + schema + '.schema_info WHERE id=1)' for schema in SCHEMAS)
    tables = []
    for name in TABLES:
        schema, table = name.split('.')
        tables.extend(["'" + name + "'", "(SELECT json_build_object("
            "'rows',count(*),'sha256',encode(sha256(convert_to(coalesce("
            "string_agg(row_to_json(t)::text,E'\\n' ORDER BY row_to_json(t)::text COLLATE \"C\"),''),"
            "'UTF8')),'hex'),'columns', (SELECT json_agg(json_build_array(column_name,udt_name,is_nullable) "
            "ORDER BY ordinal_position) FROM information_schema.columns WHERE table_schema='"
            + schema + "' AND table_name='" + table + "')) FROM " + name + " t)"])
    return "SELECT json_build_object('database',current_database(),'server_major'," \
        "current_setting('server_version_num')::int/10000,'schemas',json_build_object(" \
        + versions + "),'table_names',(SELECT json_agg(table_schema||'.'||table_name " \
        "ORDER BY table_schema,table_name) FROM information_schema.tables WHERE table_type='BASE TABLE' " \
        "AND table_schema IN (" + schema_names + ")),'tables',json_build_object(" + ','.join(tables) + '));'


def validate_inventory(value, database='moodle_pilotage'):
    if not isinstance(value, dict) or set(value) != {'database','server_major','schemas','table_names','tables'}:
        raise ValueError('Inventaire incomplet.')
    if value['database'] != database or value['server_major'] != 16:
        raise ValueError('PostgreSQL 16 et base de pilotage requis.')
    expected = {name: [1, app] for name, (app, _) in SCHEMAS.items()}
    if value['schemas'] != expected or sorted(value['table_names'] or []) != sorted(TABLES):
        raise ValueError('Schemas du pilotage absents ou incompatibles.')
    if not isinstance(value['tables'],dict) or set(value['tables']) != set(TABLES):
        raise ValueError('Tables du pilotage incompatibles.')
    for entry in value['tables'].values():
        if not isinstance(entry,dict) or set(entry) != {'rows','sha256','columns'}:
            raise ValueError('Preuve de table invalide.')
        if type(entry['rows']) is not int or entry['rows'] < 0:
            raise ValueError('Comptage invalide.')
        if not isinstance(entry['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',entry['sha256']):
            raise ValueError('Empreinte de table invalide.')
        if not isinstance(entry['columns'],list) or not entry['columns'] or any(
            not isinstance(column,list) or len(column)!=3 or any(not isinstance(v,str) for v in column)
            for column in entry['columns']):
            raise ValueError('Colonnes invalides.')
    return value


def identity_check_sql():
    checks = []
    for name in IDENTITIES:
        schema, table = name.split('.')
        checks.extend(["'"+name+"'", "(SELECT CASE WHEN COALESCE((SELECT max(id) FROM " + name + "),0)=0 "
            "THEN true ELSE last_value>(SELECT max(id) FROM " + name + ") OR "
            "(last_value=(SELECT max(id) FROM " + name + ") AND is_called) END FROM "
            + schema + '.' + table + '_id_seq)'])
    return 'SELECT json_build_object(' + ','.join(checks) + ');'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
