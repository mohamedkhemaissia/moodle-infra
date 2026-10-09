import copy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from backup_status import catalog
from backup_status.collector import publish

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def document():
    return {"schema_version": 1, "observed_at": "2026-10-09T12:00:00Z", "scopes": {
        "moodle": {"status": "available", "reason": None, "total": 2, "truncated": False,
            "latest_success_presence": "present", "latest_success_id": "a" * 64,
            "items": [{"id": "a" * 64, "archived_at": "2026-10-09T10:05:00Z",
                "recovery_at": "2026-10-09T10:00:00Z", "hostname": "moodleprod", "success_recorded": True},
                {"id": "b" * 64, "archived_at": "2026-10-08T10:05:00Z", "recovery_at": None,
                 "hostname": "moodleprod", "success_recorded": False}]},
        "pilotage": catalog.unavailable("not_configured")}}


class ContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "catalog.json"
        self.write(document())

    def write(self, value):
        self.path.write_text(json.dumps(value))

    def test_full_identifier_dates_and_pagination_preserve_different_semantics(self):
        response = catalog.summary(self.path, NOW, limit=1)
        self.assertEqual((response["total"], response["listed_total"], response["has_more"]), (2, 2, True))
        point = response["items"][0]
        self.assertEqual(point["id"], "a" * 64)
        self.assertLess(point["recovery_at"], point["archived_at"])
        older = catalog.summary(self.path, NOW, limit=1, offset=1)
        self.assertIsNone(older["items"][0]["recovery_at"])
        self.assertFalse(older["items"][0]["success_recorded"])
        self.assertFalse(older["has_more"])
        self.assertEqual(response["verification"], {"repository_integrity": "not_checked", "restoration": "not_checked"})

    def test_empty_repository_is_not_unavailable_and_missing_marker_stays_unknown(self):
        value = document()
        value["scopes"]["moodle"].update(items=[], total=0, latest_success_id=None, latest_success_presence="unknown")
        self.write(value)
        self.assertEqual(catalog.summary(self.path, NOW)["status"], "available")
        self.assertEqual(catalog.summary(self.path, NOW)["total"], 0)
        self.assertEqual(catalog.summary(self.path, NOW, scope="pilotage")["reason"], "not_configured")

    def test_stale_future_failed_collection_and_missing_files_never_keep_old_points(self):
        for moment, expected in ((NOW+timedelta(seconds=901), "collection_stale"),
                                 (NOW-timedelta(seconds=1), "clock_inconsistent")):
            value = catalog.summary(self.path, moment)
            self.assertEqual(value["reason"], expected)
            self.assertEqual(value["items"], [])
            self.assertIsNone(value["total"])
        value = document()
        value["scopes"]["moodle"] = catalog.unavailable("repository_timeout")
        self.write(value)
        self.assertEqual(catalog.summary(self.path, NOW)["reason"], "repository_timeout")
        self.path.unlink()
        self.assertEqual(catalog.summary(self.path, NOW)["reason"], "catalog_file_missing")

    def test_corruption_duplicates_dates_counts_and_unsupported_claims_are_rejected(self):
        for mutate in (
                lambda s: s["items"].append(copy.deepcopy(s["items"][0])),
                lambda s: s.update(total=True),
                lambda s: s.update(truncated=True),
                lambda s: s["items"][1].update(recovery_at="2026-10-08T09:00:00Z"),
                lambda s: s["items"][0].update(recovery_at="2026-10-09T10:06:00Z"),
                lambda s: s["items"][0].update(archived_at="2026-10-10T10:06:00Z"),
                lambda s: s["items"][0].update(hostname="private@example.test"),
                lambda s: s["items"][0].update(integrity="guaranteed"),
                lambda s: s.update(latest_success_presence="absent"),
                lambda s: s["items"].reverse()):
            value = document()
            mutate(value["scopes"]["moodle"])
            self.write(value)
            response = catalog.summary(self.path, NOW)
            self.assertEqual(response["reason"], "catalog_file_invalid")
            self.assertEqual(response["items"], [])
        for raw in ('{"schema_version":1,"schema_version":1}', '{"value":NaN}', '{"secret":"hidden"}', '['*1000):
            self.path.write_text(raw)
            response = catalog.summary(self.path, NOW)
            self.assertEqual(response["reason"], "catalog_file_invalid")
            self.assertNotIn("hidden", json.dumps(response))

    def test_links_fifos_and_oversized_documents_fail_without_following_or_blocking(self):
        target = self.path.with_name("target.json")
        self.path.rename(target)
        self.path.symlink_to(target)
        self.assertEqual(catalog.summary(self.path, NOW)["reason"], "catalog_file_invalid")
        self.path.unlink()
        import os
        os.mkfifo(self.path)
        self.assertEqual(catalog.summary(self.path, NOW)["reason"], "catalog_file_invalid")
        self.path.unlink()
        self.path.write_bytes(b"x" * (catalog.MAX_BYTES + 1))
        self.assertEqual(catalog.summary(self.path, NOW)["reason"], "catalog_file_invalid")

    def test_publication_validates_before_replacing_a_working_file(self):
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            publish(self.path, {"bad": True}, catalog.validate_document)
        self.assertEqual(self.path.read_bytes(), before)
        value = document()
        publish(self.path, value, catalog.validate_document)
        self.assertEqual(catalog.read_document(self.path), value)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)

    def test_query_bounds_and_truncation_are_explicit(self):
        for kwargs in ({"scope": "anything"}, {"limit": True}, {"limit": 51}, {"offset": -1}, {"offset": 201}):
            with self.assertRaises(ValueError):
                catalog.summary(self.path, NOW, **kwargs)
        value = document()
        entries = [{"id": f"{i:064x}", "archived_at": "2026-10-08T10:00:00Z", "recovery_at": None,
                    "hostname": "moodleprod", "success_recorded": False} for i in range(205, 5, -1)]
        value["scopes"]["moodle"].update(items=entries, total=205, truncated=True,
                                        latest_success_id=None, latest_success_presence="unknown")
        self.write(value)
        response = catalog.summary(self.path, NOW, limit=50, offset=150)
        self.assertEqual((response["total"], response["listed_total"], response["truncated"], response["has_more"]),
                         (205, 200, True, False))


if __name__ == "__main__":
    unittest.main()
