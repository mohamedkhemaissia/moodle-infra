"""Une session valide reste utilisable lorsque les requetes se depassent."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from itertools import count
import threading
import unittest
from unittest.mock import patch

from pilotage.auth import AuthDenied, AuthStore, IDLE_SECONDS, SESSION_SECONDS


class SessionCursor:
    def __init__(self, row):
        self.row = row

    def execute(self, statement, parameters):
        if statement.startswith("UPDATE sessions SET last_seen_at="):
            self.row["last_seen_at"] = parameters[0]
        return self

    def fetchone(self):
        return dict(self.row)


class SessionConcurrencyTests(unittest.TestCase):
    def setUp(self):
        # Adaptateur de transaction en memoire ; aucune base du pilote n'est ouverte.
        self.store = object.__new__(AuthStore)
        self.row = {
            "id": 1, "username": "hamma", "display_name": "Mohamed",
            "role": "admin", "active": True, "version": 1, "user_version": 1,
            "created_at": 100.0, "updated_at": 100.0,
            "session_created_at": 100.0, "last_seen_at": 100.0,
            "expires_at": 100.0 + SESSION_SECONDS,
        }
        self.cursor = SessionCursor(self.row)
        self.lock = threading.Lock()
        self.token = "a" * 43

        @contextmanager
        def connection(*, write=False):
            with self.lock:
                yield self.cursor

        self.store.connection = connection

    def test_a_later_request_can_finish_before_a_waiting_request_without_logging_out(self):
        waiting = threading.Event()
        later_finished = threading.Event()
        local = threading.local()
        original_connection = self.store.connection

        @contextmanager
        def reordered_connection(*, write=False):
            if getattr(local, "wait_for_later_request", False):
                waiting.set()
                if not later_finished.wait(10):
                    raise RuntimeError("La seconde requete n'a pas termine.")
            with original_connection(write=write) as cursor:
                yield cursor

        self.store.connection = reordered_connection

        def first_request():
            local.wait_for_later_request = True
            return self.store.authenticate(self.token)

        def later_request():
            try:
                return self.store.authenticate(self.token)
            finally:
                later_finished.set()

        clock = count(101)
        with patch("pilotage.auth.time.time", side_effect=lambda: next(clock)):
            with ThreadPoolExecutor(max_workers=2) as workers:
                first = workers.submit(first_request)
                try:
                    self.assertTrue(waiting.wait(10), "La premiere requete n'est pas en attente.")
                    later = workers.submit(later_request)
                    self.assertEqual(later.result(timeout=10)["user"]["role"], "admin")
                    self.assertEqual(first.result(timeout=10)["user"]["role"], "admin")
                finally:
                    later_finished.set()
        self.assertEqual(self.row["last_seen_at"], 102.0)

    def test_idle_and_absolute_expiration_still_reject_the_session(self):
        for instant in (100.0 + IDLE_SECONDS, 100.0 + SESSION_SECONDS):
            with self.subTest(instant=instant), self.assertRaises(AuthDenied):
                self.store.authenticate(self.token, now=instant)
        self.assertEqual(self.row["last_seen_at"], 100.0)

    def test_real_backward_clock_and_revoked_user_still_fail_closed(self):
        with self.assertRaises(AuthDenied):
            self.store.authenticate(self.token, now=99.0)
        self.row["active"] = False
        with self.assertRaises(AuthDenied):
            self.store.authenticate(self.token, now=101.0)
        self.row["active"] = True
        self.row["version"] = 2
        with self.assertRaises(AuthDenied):
            self.store.authenticate(self.token, now=101.0)
        self.assertEqual(self.row["last_seen_at"], 100.0)


if __name__ == "__main__":
    unittest.main()
