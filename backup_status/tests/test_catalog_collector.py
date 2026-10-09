from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from backup_status.catalog_collector import collect, command, ListingError, parse_time

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def row(identifier, tag, path, date="2026-10-09T10:05:00.123456789Z"):
    return {"id": identifier * 64, "tree": "f" * 64, "time": date, "paths": [path],
            "hostname": "moodleprod", "tags": [tag], "username": "private-user", "program_version": "restic test"}


class DateTests(unittest.TestCase):
    def test_fractional_precision_and_timezone_offsets_normalize_without_rounding(self):
        for fraction in ("", ".1", ".12", ".123", ".1234", ".12345", ".123456",
                         ".1234567", ".12345678", ".123456789", ".999999999"):
            for date, expected in (
                ("2026-10-09T10:05:00"+fraction+"Z", "2026-10-09T10:05:00Z"),
                ("2026-10-09T10:05:00"+fraction+"+01:00", "2026-10-09T09:05:00Z"),
                ("2026-10-09T23:59:59"+fraction+"-01:30", "2026-10-10T01:29:59Z")):
                with self.subTest(date=date):
                    self.assertEqual(parse_time(date), expected)

    def test_malformed_dates_offsets_and_out_of_range_utc_are_rejected(self):
        for date in (None, 1791540000, "2026-10-09T10:05:00", "2026-10-09T10:05:00.Z",
                     "2026-10-09T10:05:00.1234567890Z", "2026-10-09T10:05:00.1Zjunk",
                     "2026-10-09T10:05:00+24:00", "2026-10-09T10:05:00+01:60",
                     "2026-02-30T10:05:00Z", "2026-10-09T24:05:00Z",
                     "0001-01-01T00:00:00+23:00", "9999-12-31T23:59:59-23:00"):
            with self.subTest(date=date), self.assertRaises(ValueError):
                parse_time(date)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.marker = self.root / "last-success.txt"
        self.pgroot = self.root / "pilotage"
        self.pgroot.mkdir()
        self.rows = [row("a", "moodle-pilote-full", "/opt/moodle/backups/staging/run-20261009.foo"),
                     row("b", "moodle-pilotage-database", str(self.pgroot)+"/staging/run-abc"),
                     row("c", "other", "/private/path")]
        self.marker.write_text("snapshot="+"a"*64+"\nfrozen_at_utc=2026-10-09T10:00:00Z\ncompleted_at_utc=2026-10-09T10:06:00Z\n")
        self.calls = []

    def runner(self, argv):
        self.calls.append(argv)
        return json.dumps(self.rows).encode()

    def collect(self, run=None):
        return collect("moodle-pilote", "/opt/moodle/backups/staging", self.marker, self.pgroot,
                       run=run or self.runner, clock=lambda: NOW)

    def test_only_matching_tags_and_paths_are_exposed_without_secrets(self):
        value = self.collect()
        self.assertEqual([self.calls[0]], [["/usr/local/sbin/moodle-pilote-restic", "snapshots", "--json", "--no-lock", "--no-cache"]])
        self.assertEqual(value["scopes"]["moodle"]["total"], 1)
        self.assertEqual(value["scopes"]["pilotage"]["total"], 1)
        point = value["scopes"]["moodle"]["items"][0]
        self.assertEqual(point["recovery_at"], "2026-10-09T10:00:00Z")
        self.assertEqual(point["archived_at"], "2026-10-09T10:05:00Z")
        for hidden in ("/private", "private-user", "paths", "username", "tree", "program_version", str(self.root)):
            self.assertNotIn(hidden, json.dumps(value))

    def test_missing_remote_point_remains_absent_and_old_times_are_not_invented(self):
        self.rows.pop(0)
        value = self.collect()["scopes"]["moodle"]
        self.assertEqual((value["total"], value["latest_success_presence"]), (0, "absent"))
        self.marker.unlink()
        self.rows.insert(0, row("a", "moodle-pilote-full", "/opt/moodle/backups/staging/run-abc"))
        value = self.collect()["scopes"]["moodle"]
        self.assertIsNone(value["items"][0]["recovery_at"])
        self.assertEqual(value["latest_success_presence"], "unknown")

    def test_wrong_scope_or_future_point_blocks_only_the_affected_scope(self):
        for path in ("/opt/another/run-a", "/opt/moodle/backups/staging/run-a/../private"):
            self.rows[0]["paths"] = [path]
            value = self.collect()["scopes"]
            self.assertEqual(value["moodle"]["reason"], "scope_metadata_invalid")
            self.assertEqual(value["pilotage"]["status"], "available")
        self.rows[0] = row("a", "moodle-pilote-full", "/opt/moodle/backups/staging/run-a", "2026-10-09T13:00:00Z")
        self.assertEqual(self.collect()["scopes"]["moodle"]["status"], "unavailable")

    def test_listing_errors_invalid_json_and_duplicates_replace_success_with_unknown(self):
        for reason in ("repository_unavailable", "repository_timeout", "repository_output_too_large"):
            def fail(argv):
                raise ListingError(reason)
            value = self.collect(fail)
            self.assertEqual(value["scopes"]["moodle"]["reason"], reason)
            self.assertEqual(value["scopes"]["moodle"]["items"], [])
        for raw in (b"[]\nsecret", b"{}", b"[NaN]", json.dumps([self.rows[0], self.rows[0]]).encode(),
                    b'[{"id":"a","id":"b"}]'):
            value = self.collect(lambda argv: raw)
            self.assertEqual(value["scopes"]["moodle"]["reason"], "repository_output_invalid")

    def test_config_does_not_allow_command_or_path_injection(self):
        for project in ("../bad", "x;command", "x$(echo)", "-option"):
            with self.assertRaises(ValueError):
                collect(project, "/opt/moodle/backups/staging", self.marker, run=self.runner)
        self.assertEqual(self.calls, [])

    def test_collection_timestamp_is_the_beginning_of_the_read(self):
        times = iter([NOW, datetime(2026, 10, 9, 12, 8, tzinfo=timezone.utc)])
        value = collect("moodle-pilote", "/opt/moodle/backups/staging", self.marker,
                        run=self.runner, clock=lambda: next(times))
        self.assertEqual(value["observed_at"], "2026-10-09T12:00:00Z")


if __name__ == "__main__":
    unittest.main()
