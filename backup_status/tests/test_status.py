import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backup_status.collector import collect, publish, unit_states
from backup_status.status import MAX_BYTES, marker, summary

NOW = datetime(2026, 10, 8, 21, 0, tzinfo=timezone.utc)
LATEST = {"snapshot": "a" * 64, "frozen_at_utc": "2026-10-08T09:00:00Z",
          "completed_at_utc": "2026-10-08T09:05:00Z"}


def document():
    return {"schema_version": 1, "observed_at": "2026-10-08T21:00:00Z",
            "marker": {"status": "recorded", "latest": dict(LATEST)},
            "policy": {"max_age_hours": 24},
            "schedule": {"state": "active", "enabled": True},
            "job": {"state": "idle", "last_result": "success"}}


def systemd_output(active="inactive", result="success", timestamp="Thu 2026-10-08 09:00:00 UTC"):
    return "Id=moodle-pilote-backup.service\nLoadState=loaded\nActiveState=" + active + (
        "\nResult=" + result + "\nExecMainStatus=0\nExecMainStartTimestamp=" + timestamp +
        "\n\nId=moodle-pilote-backup.timer\nLoadState=loaded\nActiveState=active\nUnitFileState=enabled\n")


class StatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "status.json"
        self.marker = Path(temporary.name) / "last-success.txt"

    def write(self, value=None):
        self.path.write_text(json.dumps(document() if value is None else value))

    def test_age_is_measured_from_freeze_not_completion_and_does_not_claim_integrity(self):
        self.write()
        result = summary(self.path, NOW)
        self.assertEqual((result["backup_state"], result["latest_success"]["age_hours"]), ("recent", 12))
        self.assertEqual(result["latest_success"]["duration_seconds"], 300)
        self.assertEqual(result["verification"], {"repository_integrity": "not_checked", "restoration": "not_checked"})

    def test_threshold_exactly_24_hours_and_overdue_point(self):
        value = document()
        value["marker"]["latest"].update(frozen_at_utc="2026-10-07T21:00:00Z",
                                         completed_at_utc="2026-10-07T21:05:00Z")
        self.write(value)
        self.assertEqual(summary(self.path, NOW)["backup_state"], "recent")
        self.assertEqual(summary(self.path, NOW + timedelta(seconds=1))["backup_state"], "overdue")

    def test_stopped_collection_hides_previous_recent_point_and_job(self):
        self.write()
        self.assertEqual(summary(self.path, NOW + timedelta(seconds=150))["status"], "available")
        result = summary(self.path, NOW + timedelta(seconds=151))
        self.assertEqual((result["status"], result["reason"]), ("unavailable", "collection_stale"))
        self.assertIsNone(result["latest_success"])
        self.assertEqual(result["schedule"]["state"], "unknown")
        self.assertEqual(result["job"]["last_result"], "unknown")

    def test_clock_going_back_does_not_report_recent(self):
        self.write()
        result = summary(self.path, NOW - timedelta(seconds=1))
        self.assertEqual((result["backup_state"], result["reason"]), ("unknown", "clock_inconsistent"))

    def test_missing_file_and_missing_marker_are_distinct(self):
        self.assertEqual(summary(self.path, NOW)["reason"], "status_file_missing")
        value = document()
        value["marker"] = {"status": "missing", "latest": None}
        self.write(value)
        result = summary(self.path, NOW)
        self.assertEqual((result["status"], result["backup_state"]), ("available", "missing"))

    def test_malformed_marker_duplicate_dates_and_extra_fields_do_not_leak(self):
        cases = [
            "snapshot=" + "a" * 64 + "\nsnapshot=" + "a" * 64,
            "\n".join(k + "=" + v for k, v in {**LATEST, "password": "secret-test"}.items()),
            "\n".join(k + "=" + v for k, v in {**LATEST, "snapshot": "invalid"}.items()),
            "\n".join(k + "=" + v for k, v in {**LATEST, "completed_at_utc": "2026-10-09T00:00:00Z"}.items()),
            "\n".join(k + "=" + v for k, v in {**LATEST, "completed_at_utc": "2026-10-08T08:00:00Z"}.items()),
        ]
        for text in cases:
            with self.subTest(text=text[:20]):
                self.marker.write_text(text)
                self.assertEqual(marker(self.marker, NOW), {"status": "invalid", "latest": None})

    def test_nonstandard_json_duplicates_extra_fields_and_nonfinite_values_are_rejected(self):
        base = json.dumps(document())
        cases = ['{"schema_version":1,"schema_version":1}',
                 base.replace('"max_age_hours": 24', '"max_age_hours": NaN'),
                 base.replace('"max_age_hours": 24', '"max_age_hours": true'),
                 base[:-1] + ', "password": "secret-test"}', "{broken", "[]"]
        for text in cases:
            self.path.write_text(text)
            result = summary(self.path, NOW)
            self.assertEqual((result["backup_state"], result["reason"]), ("unknown", "status_file_invalid"))
            self.assertNotIn("secret-test", json.dumps(result))
            self.assertNotIn(str(self.path), json.dumps(result))

    def test_future_completion_and_invalid_public_contracts_are_rejected(self):
        cases = []
        for path, value in [
            (("marker", "latest", "completed_at_utc"), "2026-10-09T00:00:00Z"),
            (("policy", "max_age_hours"), 0), (("schema_version",), True),
            (("schedule", "enabled"), "true"), (("job", "last_result"), "guaranteed"),
        ]:
            doc = document()
            target = doc
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            cases.append(doc)
        for value in cases:
            self.write(value)
            self.assertEqual(summary(self.path, NOW)["reason"], "status_file_invalid")

    def test_oversized_symlink_and_fifo_inputs_are_not_read(self):
        self.path.write_text("x" * (MAX_BYTES + 1))
        self.assertEqual(summary(self.path, NOW)["reason"], "status_file_invalid")
        self.path.unlink()
        self.marker.write_text("private")
        self.path.symlink_to(self.marker)
        self.assertEqual(summary(self.path, NOW)["reason"], "status_file_invalid")
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertEqual(summary(self.path, NOW)["reason"], "status_file_invalid")

    def test_failed_new_job_preserves_last_recorded_point(self):
        value = document()
        value["job"]["last_result"] = "failed"
        self.write(value)
        result = summary(self.path, NOW)
        self.assertEqual(result["latest_success"]["snapshot"], LATEST["snapshot"])
        self.assertEqual(result["job"]["last_result"], "failed")

    def test_collection_uses_only_systemctl_show_and_publishes_atomic_public_data(self):
        self.marker.write_text("\n".join(k + "=" + v for k, v in LATEST.items()) + "\n")
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout=systemd_output())
        value = collect(self.marker, "moodle-pilote", 24, NOW, run)
        publish(self.path, value)
        self.assertEqual(summary(self.path, NOW)["backup_state"], "recent")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][:2], ["/usr/bin/systemctl", "show"])
        self.assertFalse(calls[0][1].get("shell", False))
        self.assertNotIn("start", calls[0][0])
        self.assertNotIn("stop", calls[0][0])

    def test_failed_publication_preserves_existing_status(self):
        self.write()
        before = self.path.read_bytes()
        with patch("backup_status.collector.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                publish(self.path, document())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob(".status-*")), [])

    def test_publication_rejects_a_symlink_destination(self):
        self.marker.write_text("unchanged")
        self.path.symlink_to(self.marker)
        with self.assertRaises(ValueError):
            publish(self.path, document())
        self.assertEqual(self.marker.read_text(), "unchanged")


class ServiceTests(unittest.TestCase):
    def test_loaded_timer_and_last_completed_job(self):
        schedule, job = unit_states("moodle-pilote", lambda *a, **k:
                                   SimpleNamespace(returncode=0, stdout=systemd_output()))
        self.assertEqual(schedule, {"state": "active", "enabled": True})
        self.assertEqual(job, {"state": "idle", "last_result": "success"})

    def test_never_run_service_does_not_claim_a_success(self):
        _, job = unit_states("moodle-pilote", lambda *a, **k:
                             SimpleNamespace(returncode=0, stdout=systemd_output(timestamp="")))
        self.assertEqual(job["last_result"], "unknown")

    def test_failed_and_running_jobs_are_distinct(self):
        for state, result, expected in [("failed", "exit-code", "failed"), ("activating", "success", "unknown")]:
            _, job = unit_states("moodle-pilote", lambda *a, **k:
                                 SimpleNamespace(returncode=0, stdout=systemd_output(state, result)))
            self.assertEqual(job["last_result"], expected)
            self.assertEqual(job["state"], "running" if state == "activating" else "idle")

    def test_timeouts_errors_and_unexpected_units_are_unknown(self):
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired("systemctl", 15)
        for run in [timeout, lambda *a, **k: SimpleNamespace(returncode=1, stdout="private"),
                    lambda *a, **k: SimpleNamespace(returncode=0, stdout=systemd_output().replace(
                        "moodle-pilote-backup.timer", "unexpected.timer"))]:
            schedule, job = unit_states("moodle-pilote", run)
            self.assertEqual(schedule["state"], "unknown")
            self.assertEqual(job["last_result"], "unknown")

    def test_project_name_cannot_inject_a_command_or_unrelated_unit(self):
        with self.assertRaises(ValueError):
            unit_states("moodle;reboot", lambda *a, **k: self.fail("Commande executee."))


if __name__ == "__main__":
    unittest.main()
