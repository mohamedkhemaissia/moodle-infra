"""Compatibilite des lectures avec les curseurs utilises par les incidents."""
import sqlite3
from types import SimpleNamespace
import unittest

from pilotage.db import Cursor


class CursorTests(unittest.TestCase):
    def cursor(self, statement):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        raw = connection.execute(statement)
        # Psycopg expose les noms des colonnes par attribut, SQLite par tuple.
        description = [SimpleNamespace(name=column[0]) for column in raw.description]
        return Cursor(SimpleNamespace(description=description, fetchone=raw.fetchone,
                                      fetchall=raw.fetchall))

    def test_iteration_reads_named_rows_and_empty_results(self):
        cursor = self.cursor("SELECT 'open' AS status, 2 AS count UNION ALL SELECT 'resolved', 3")
        self.assertEqual({row['status']: row['count'] for row in cursor},
                         {'open': 2, 'resolved': 3})
        self.assertEqual(list(cursor), [])
        self.assertEqual(list(self.cursor('SELECT 1 AS id WHERE 0')), [])

    def test_fetch_and_iteration_share_the_same_position(self):
        cursor = self.cursor('SELECT 1 AS id UNION ALL SELECT 2 UNION ALL SELECT 3')
        self.assertEqual(dict(cursor.fetchone()), {'id': 1})
        rows = [dict(row) for row in cursor]
        self.assertEqual(rows, [{'id': 2}, {'id': 3}])
        self.assertIsNone(cursor.fetchone())
        self.assertEqual(cursor.fetchall(), [])


if __name__ == '__main__':
    unittest.main()
