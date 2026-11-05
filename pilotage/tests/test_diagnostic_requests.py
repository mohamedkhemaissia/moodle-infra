import unittest

from pilotage.diagnostic_requests import DiagnosticRequestStore, public_request


def row(*, available):
    return {
        "id": "a" * 32,
        "incident_id": 7,
        "status": "failed",
        "actor_id": 1,
        "actor_name": "admin",
        "created_at": 1,
        "updated_at": 3,
        "started_at": 2,
        "finished_at": 3,
        "run_id": "a" * 32,
        "result_available": available,
        "reason_code": "diagnostic_worker_failed",
    }


class DiagnosticRequestProjectionTests(unittest.TestCase):
    def test_result_link_is_exposed_only_after_the_diagnostic_audit_exists(self):
        missing = public_request(row(available=False))
        self.assertIsNone(missing["run_id"])
        self.assertIsNone(missing["result_url"])
        persisted = public_request(row(available=True))
        self.assertEqual(persisted["run_id"], "a" * 32)
        self.assertEqual(persisted["result_url"], "/api/v1/diagnostics/" + "a" * 32)

    def test_terminal_without_audit_requires_a_safe_reason(self):
        store = DiagnosticRequestStore.__new__(DiagnosticRequestStore)
        with self.assertRaises(ValueError):
            store.finish("a" * 32, "b" * 32, "failed", result_available=False)
        with self.assertRaises(ValueError):
            store.finish(
                "a" * 32, "b" * 32, "completed",
                reason_code="diagnostic_result_unavailable", result_available=False,
            )


if __name__ == "__main__":
    unittest.main()
