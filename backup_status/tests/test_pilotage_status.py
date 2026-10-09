from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backup_status import pilotage
from backup_status.collector import main, publish

NOW = datetime(2026, 10, 9, 8, tzinfo=timezone.utc)
SCHEDULE = {"state": "active", "enabled": True}
JOB = {"state": "idle", "last_result": "success"}


def marker():
    return {"format": "moodle-pilotage-backup-v1", "status": "backed_up", "snapshot": "a" * 64,
        "started_at": "2026-10-09T07:08:32Z", "captured_at": "2026-10-09T07:08:36Z",
        "completed_at": "2026-10-09T07:08:46Z", "repository_check": "passed", "tables": 16,
        "rows": 415, "source_path": "/private/staging/secret", "manifest_sha256": "b" * 64,
        "restoration": "not_tested"}


def report():
    return {"status": "restore_validated", "mode": "isolated_restore_test", "run_id": "c" * 32,
        "snapshot": "a" * 64, "started_at": "2026-10-09T07:16:41Z",
        "completed_at": "2026-10-09T07:17:58Z", "captured_at": "2026-10-09T07:08:36Z",
        "production_changed": False, "tables_verified": 16, "rows_verified": 415,
        "sequence_check": "passed", "inventory_sha256": "d" * 64, "duration_seconds": 76.819,
        "cleanup": {"container_removed": True, "temporary_files_removed": True},
        "report_file": "/private/reports/secret"}


class PilotageStatusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.marker = self.root / "last-success.json"
        self.public = self.root / "public.json"
        self.marker.write_text(json.dumps(marker()))

    def write_report(self, value=None):
        value = report() if value is None else value
        path = self.reports / ("restore-" + value["run_id"] + ".json")
        path.write_text(json.dumps(value))
        return path

    def collect(self, now=NOW):
        return pilotage.collect(self.root, 24, now, SCHEDULE, JOB)

    def summary(self, now=NOW):
        publish(self.public, self.collect(), pilotage.validate_document)
        return pilotage.summary(self.public, now)

    def test_real_backup_and_restore_shapes_project_without_private_fields(self):
        self.write_report()
        value = self.summary()
        self.assertEqual(value["backup_state"], "recent")
        self.assertEqual(value["restoration_state"], "validated")
        self.assertTrue(value["same_snapshot"])
        self.assertEqual(value["latest_success"]["tables"], 16)
        self.assertEqual(value["latest_restore"]["rows_verified"], 415)
        self.assertEqual(value["verification"]["repository_integrity"], "passed_at_backup")
        for data in (self.collect(), value):
            text = json.dumps(data)
            for private in ("/private", "source_path", "report_file", "inventory_sha256", "manifest_sha256"):
                self.assertNotIn(private, text)
        self.assertEqual(self.public.stat().st_mode & 0o777, 0o644)

    def test_old_restore_does_not_validate_new_snapshot(self):
        value = report()
        value["snapshot"] = "b" * 64
        self.write_report(value)
        data = self.summary()
        self.assertEqual(data["latest_restore"]["status"], "restore_validated")
        self.assertFalse(data["same_snapshot"])
        self.assertEqual(data["restoration_state"], "not_tested")

    def test_latest_completed_report_wins_not_mtime_or_prior_success(self):
        old = self.write_report()
        failure = report()
        failure.update(run_id="e" * 32, status="failed", started_at="2026-10-09T07:30:00Z",
                       completed_at="2026-10-09T07:31:00Z")
        self.write_report(failure)
        os.utime(old, (NOW.timestamp(), NOW.timestamp()))
        self.assertEqual(self.summary()["restoration_state"], "failed")

    def test_no_tests_and_no_marker_are_distinct_from_no_collector(self):
        self.assertEqual(self.summary()["restoration_state"], "not_tested")
        self.marker.unlink()
        data = self.summary()
        self.assertEqual((data["status"], data["backup_state"]), ("available", "missing"))
        self.public.unlink()
        data = pilotage.summary(self.public, NOW)
        self.assertEqual((data["status"], data["reason"]), ("unavailable", "status_file_missing"))

    def test_stale_and_future_publication_hide_all_proofs(self):
        self.write_report()
        for moment, reason in ((NOW + timedelta(seconds=151), "collection_stale"),
                               (NOW - timedelta(seconds=1), "clock_inconsistent")):
            data = self.summary(moment)
            self.assertEqual(data["reason"], reason)
            self.assertIsNone(data["latest_success"])
            self.assertIsNone(data["latest_restore"])
            self.assertEqual(data["restoration_state"], "unknown")

    def test_corrupt_report_does_not_reuse_an_old_success(self):
        self.write_report()
        (self.reports / ("restore-" + "e" * 32 + ".json")).write_text('{"secret":"private"}')
        data = self.summary()
        self.assertEqual(data["restore_collection_status"], "invalid")
        self.assertEqual(data["restoration_state"], "unknown")
        self.assertIsNone(data["latest_restore"])

    def test_false_validation_wrong_counts_or_unfinished_cleanup_cannot_be_green(self):
        for mutate in (lambda x: x.update(rows_verified=414), lambda x: x.update(tables_verified=15),
                       lambda x: x.update(captured_at="2026-10-09T07:08:35Z"),
                       lambda x: x["cleanup"].update(container_removed=False),
                       lambda x: x.update(sequence_check="not_verified"),
                       lambda x: x.update(production_changed=True)):
            with self.subTest(mutate=mutate):
                value = report()
                mutate(value)
                self.write_report(value)
                self.assertEqual(self.summary()["restoration_state"], "unknown")

    def test_untrusted_symlinks_fifos_large_and_nonstandard_json_are_rejected(self):
        original = self.marker.read_text()
        self.marker.unlink()
        private = self.root / "private.json"
        private.write_text(original)
        self.marker.symlink_to(private)
        self.assertEqual(self.collect()["marker"]["status"], "invalid")
        self.marker.unlink()
        os.mkfifo(self.marker)
        self.assertEqual(self.collect()["marker"]["status"], "invalid")
        self.marker.unlink()
        for text in ('{"status":"backed_up","status":"backed_up"}',
                     '{"value":NaN}', 'x' * 8193):
            self.marker.write_text(text)
            self.assertEqual(self.collect()["marker"]["status"], "invalid")
        self.marker.write_text(original)
        self.write_report().unlink()
        (self.reports / ("restore-" + "c" * 32 + ".json")).symlink_to(private)
        self.assertEqual(self.collect()["restore"]["status"], "invalid")

    def test_public_schema_rejects_extra_fields_booleans_dates_and_invalid_policy(self):
        good = self.collect()
        for mutate in (lambda x: x.update(secret="private"),
                       lambda x: x["marker"]["latest"].update(secret="private"),
                       lambda x: x["marker"]["latest"].update(tables=True),
                       lambda x: x["marker"]["latest"].update(completed_at_utc="2099-01-01T00:00:00Z"),
                       lambda x: x["policy"].update(max_age_hours=float("inf"))):
            value = deepcopy(good)
            mutate(value)
            with self.assertRaises(ValueError):
                pilotage.validate_document(value)

    def test_report_limit_is_explicit_and_bounded(self):
        self.write_report()
        other = report()
        other["run_id"] = "e" * 32
        self.write_report(other)
        with patch.object(pilotage, "MAX_REPORTS", 1):
            self.assertEqual(self.collect()["restore"]["status"], "invalid")

    def test_failed_job_keeps_the_last_success_and_overdue_age_uses_capture_time(self):
        value = self.collect()
        value["job"]["last_result"] = "failed"
        value["policy"]["max_age_hours"] = 0.1
        publish(self.public, value, pilotage.validate_document)
        data = pilotage.summary(self.public, NOW)
        self.assertEqual(data["backup_state"], "overdue")
        self.assertEqual(data["job"]["last_result"], "failed")
        self.assertIsNotNone(data["latest_success"])

    def test_collector_option_only_reads_units_and_publishes_both_public_files(self):
        moodle_marker = self.root / "moodle.txt"
        moodle_marker.write_text("snapshot=" + "a" * 64 + "\nfrozen_at_utc=2026-10-09T07:08:36Z\ncompleted_at_utc=2026-10-09T07:08:46Z\n")
        output = self.root / "status.json"
        args = ["collector", "--marker", str(moodle_marker), "--project", "moodle-pilote",
                "--output", str(output), "--pilotage-root", str(self.root)]
        with patch("sys.argv", args), patch("backup_status.collector.unit_states", return_value=(SCHEDULE, JOB)) as units:
            main()
        self.assertTrue(output.is_file())
        pg = self.root / "pilotage.json"
        self.assertEqual(json.loads(pg.read_text())["scope"], "pilotage_postgresql")
        units.assert_called_with("moodle-pilotage")


if __name__ == "__main__":
    unittest.main()
