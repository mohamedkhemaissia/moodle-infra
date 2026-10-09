import sys
import time
import unittest
from backup_status.catalog_collector import command, ListingError


class ProcessTests(unittest.TestCase):
    def test_nonzero_exit_timeout_and_large_output_are_bounded_and_sanitized(self):
        # Seul le cas timeout a besoin d'un delai court. Laisser Python demarrer
        # dans les autres cas permet de tester l'erreur visee, meme sur un runner charge.
        for source, reason, size, deadline in (
                ("import sys;print('password');sys.exit(1)", "repository_unavailable", 1024, 5),
                ("import time;time.sleep(10)", "repository_timeout", 1024, .25),
                ("print('x'*5000)", "repository_output_too_large", 1024, 5)):
            start = time.monotonic()
            with self.assertRaises(ListingError) as exc:
                command([sys.executable, "-c", source], timeout=deadline, max_bytes=size)
            self.assertEqual(str(exc.exception), reason)
            self.assertLess(time.monotonic()-start, deadline+4)
        self.assertEqual(command([sys.executable, "-c", "print('[]')"]), b"[]\n")

