from datetime import datetime, timezone, timedelta
import copy
import json
from pathlib import Path
import tempfile
import unittest
from backup_status.catalog import select_point, unavailable


class CatalogSelectionTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.path=Path(directory.name)/'catalog.json'; self.now=datetime(2026,10,9,12,tzinfo=timezone.utc)
        self.point=dict(id='a'*64,archived_at='2026-10-09T11:00:00Z',recovery_at=None,hostname='moodleprod',success_recorded=False)
        scope=dict(status='available',reason=None,total=1,items=[self.point],truncated=False,
                   latest_success_presence='unknown',latest_success_id=None)
        self.value=dict(schema_version=1,observed_at='2026-10-09T12:00:00Z',scopes={'moodle':copy.deepcopy(scope),'pilotage':copy.deepcopy(scope)})
        self.publish()
    def publish(self): self.path.write_text(json.dumps(self.value))
    def test_selection_keeps_origin_and_unknown_capture_without_claiming_a_restore(self):
        result=select_point(self.path,self.now,'moodle','a'*64)
        self.assertEqual(result,dict(scope='moodle',point=self.point,catalog_observed_at=self.value['observed_at']))
        self.assertIsNone(result['point']['recovery_at'])
    def test_missing_reference_stale_future_symlink_and_invalid_documents_block_selection(self):
        for scope, identifier, now in [('all','a'*64,self.now),('moodle','a'*12,self.now),('moodle','b'*64,self.now),
             ('moodle','a'*64,self.now+timedelta(seconds=901)),('moodle','a'*64,self.now-timedelta(seconds=1))]:
            with self.subTest(scope=scope,identifier=identifier,now=now),self.assertRaises(ValueError):
                select_point(self.path,now,scope,identifier)
        link=self.path.parent/'link'; link.symlink_to(self.path)
        with self.assertRaises(ValueError): select_point(link,self.now,'moodle','a'*64)
        self.path.write_text('{"schema_version":1,"schema_version":1}')
        with self.assertRaises(ValueError): select_point(self.path,self.now,'moodle','a'*64)
    def test_a_failure_in_the_other_repository_does_not_block_this_scope(self):
        self.value['scopes']['pilotage']=unavailable('repository_unavailable'); self.publish()
        self.assertEqual(select_point(self.path,self.now,'moodle','a'*64)['scope'],'moodle')
        with self.assertRaises(ValueError): select_point(self.path,self.now,'pilotage','a'*64)


if __name__=='__main__':unittest.main()
