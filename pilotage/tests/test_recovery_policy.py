import copy
import unittest
from types import SimpleNamespace
from pilotage.auth import AuthDenied, IDLE_SECONDS
from pilotage.recovery import authorize, identifier, note, public_request

BASE = 1791547200


class RecoveryPolicyTests(unittest.TestCase):
    def test_only_complete_identifiers_and_bounded_plain_motives_are_accepted(self):
        self.assertEqual(identifier('a'*64, 64), 'a'*64)
        self.assertEqual(note('  Tester la copie isolee  '), 'Tester la copie isolee')
        for value in ('a'*12, 'A'*32, 'x/../path', None, 1):
            with self.subTest(value=value), self.assertRaises(ValueError): identifier(value)
        for value in ('trop court', 'x'*501, 'Tester\nla restauration', 'Confirmer\x7f le point', None):
            with self.subTest(value=value), self.assertRaises(ValueError): note(value)

    def test_transaction_revalidates_identity_role_revocation_and_clock(self):
        principal = {'kind':'human', 'user':{'id':1}, 'session_hash':'b'*64, 'version':1}
        row = dict(id=1, username='hamma', active=True, role='admin', version=1, user_version=1,
                   created_at=BASE-10, last_seen_at=BASE-1, expires_at=BASE+100)
        cursor = SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchone=lambda: row))
        self.assertEqual(authorize(cursor, principal, BASE)['id'], 1)
        for changes in ({'active':False}, {'role':'reader'}, {'version':2}, {'user_version':2},
                        {'expires_at':BASE}, {'last_seen_at':BASE+1}, {'last_seen_at':BASE-IDLE_SECONDS}):
            original = dict(row); row.update(changes)
            with self.subTest(changes=changes), self.assertRaises(AuthDenied): authorize(cursor, principal, BASE)
            row.clear(); row.update(original)
        for invalid in (None, {'kind':'worker'}, {**principal, 'session_hash':'x'}, {**principal, 'version':2}):
            with self.subTest(principal=invalid), self.assertRaises(AuthDenied): authorize(cursor, invalid, BASE)
        cursor.execute = lambda *args: SimpleNamespace(fetchone=lambda: None)
        with self.assertRaises(AuthDenied): authorize(cursor, principal, BASE)

    def test_public_approval_expires_and_never_becomes_an_execution_claim(self):
        row = dict(id='a'*32, scope='pilotage', snapshot_id='b'*64, target='isolated_test', reason='Tester la copie isolee',
            status='approved', revision=2, actor_id=1, actor_name='hamma', created_at=BASE-1, updated_at=BASE,
            selected_json='{}', decision_actor_id=1, decision_actor_name='hamma', decision_reason='Verifier cette sauvegarde',
            decision_at=BASE, valid_until=BASE+900, session_hash='SECRET', payload_sha256='PRIVATE')
        self.assertTrue(public_request(row, BASE)['decision']['approval_valid'])
        for now in (BASE-1, BASE+900):
            public = public_request(row, now)
            self.assertFalse(public['decision']['approval_valid'])
            self.assertEqual(public['execution'], {'enabled':False, 'status':'not_dispatched'})
            self.assertNotIn('session_hash', public)
            self.assertNotIn('payload_sha256', public)


if __name__ == '__main__': unittest.main()
