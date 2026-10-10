import copy
import unittest
from pilotage_backup.inventory import inventory_sql,validate_inventory,identity_check_sql,identities
from test_backup import valid_inventory


class PolicyInventoryTests(unittest.TestCase):
    def test_new_policy_tables_are_included_without_changing_old_backup_profiles(self):
        old=valid_inventory();self.assertEqual(validate_inventory(old),old)
        self.assertNotIn("pilotage_policy",inventory_sql())
        current=copy.deepcopy(old)
        current['schemas']['pilotage_policy']=[1,0x4D504F4C]
        names=['schema_info','settings','events','approvals','authorizations','worker_attempts','worker_state']
        for table in names:
            name='pilotage_policy.'+table;current['table_names'].append(name)
            current['tables'][name]={'rows':1,'sha256':'a'*64,'columns':[['id','int8','NO']]}
        self.assertEqual(validate_inventory(current),current)
        for table in names:self.assertIn('pilotage_policy.'+table,inventory_sql(include_policy=True))
        self.assertIn('pilotage_policy.events',identity_check_sql(include_policy=True))
        self.assertIn('pilotage_policy.events',identities(include_policy=True))
        current['table_names'].remove('pilotage_policy.approvals')
        with self.assertRaises(ValueError):validate_inventory(current)
