import unittest

from diagnostics.worker import process


class Store:
    def __init__(self):
        self.finished = None
        self.requeued = None

    def claim(self, worker_id):
        self.worker_id = worker_id
        return {"id": "a" * 32, "incident_id": 7, "run_id": "a" * 32, "claim_token": "b" * 32}

    def finish(self, request_id, claim_token, status, *, reason_code=None, result_available=False):
        self.finished = (request_id, claim_token, status, reason_code, result_available)
        return {"status": status}

    def requeue(self, request_id, claim_token):
        self.requeued = (request_id, claim_token)


class DiagnosticWorkerTests(unittest.TestCase):
    def test_completed_job_keeps_the_request_identifier_as_run_identifier(self):
        store = Store()
        calls = []

        def execute(config, path, incident_id, *, run_identifier):
            calls.append((config, path, incident_id, run_identifier))
            return {"status": "completed", "run_id": run_identifier}, 0

        result = process(store, {"data_dir": "/fixture"}, "/fixture/config.json", "c" * 32,
                         execute_function=execute)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(calls[0][2:], (7, "a" * 32))
        self.assertEqual(store.finished, ("a" * 32, "b" * 32, "completed", None, True))

    def test_busy_runner_requeues_without_fabricating_a_result(self):
        store = Store()
        result = process(store, {}, "/fixture/config.json", "c" * 32,
                         execute_function=lambda *args, **kwargs: ({"status": "busy", "reason": "diagnostic_already_running"}, 2))
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(store.requeued, ("a" * 32, "b" * 32))
        self.assertIsNone(store.finished)

    def test_worker_failure_is_persisted_as_a_safe_code(self):
        store = Store()

        def fail(*args, **kwargs):
            raise RuntimeError("secret runner output")

        result = process(store, {}, "/fixture/config.json", "c" * 32, execute_function=fail)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(store.finished, ("a" * 32, "b" * 32, "failed", "diagnostic_worker_failed", False))


if __name__ == "__main__":
    unittest.main()
