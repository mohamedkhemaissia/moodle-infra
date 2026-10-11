import copy
import unittest
from pilotage_backup.inventory import inventory_sql, validate_inventory, identity_check_sql, identities
from test_backup import valid_inventory


class DecisionInventoryTests(unittest.TestCase):
    def test_journal_is_included_and_legacy_snapshots_still_validate(self):
        old = valid_inventory()
        self.assertEqual(validate_inventory(old), old)
        self.assertNotIn('pilotage_decisions', inventory_sql())
        current = copy.deepcopy(old)
        current['schemas']['pilotage_decisions'] = [1, 0x4D444543]
        for table in ('schema_info', 'entries', 'state'):
            name = 'pilotage_decisions.' + table
            current['table_names'].append(name)
            current['tables'][name] = {'rows': 1, 'sha256': 'a' * 64, 'columns': [['id', 'int8', 'NO']]}
            self.assertIn(name, inventory_sql(include_decisions=True))
        self.assertEqual(validate_inventory(current), current)
        self.assertIn('pilotage_decisions.entries', identities(include_decisions=True))
        self.assertIn('pilotage_decisions.entries_id_seq', identity_check_sql(include_decisions=True))
        del current['tables']['pilotage_decisions.state']
        with self.assertRaises(ValueError):
            validate_inventory(current)
