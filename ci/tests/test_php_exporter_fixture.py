"""Verifier que la fixture de regression produit le vrai protocole FastCGI."""
from pathlib import Path
import json
import socket
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_php_exporter_deadline_test import fake_status_server, read_exact, record


def request(connection):
    connection.sendall(record(1, 1, b'\0\1\0' + b'\0' * 5) + record(4, 1) + record(5, 1))


def response(connection):
    chunks = []
    while True:
        version, kind, request_id, length, padding, _ = struct.unpack('!BBHHBB', read_exact(connection, 8))
        if version != 1 or request_id != 1:
            raise AssertionError('En-tete FastCGI invalide')
        body = read_exact(connection, length)
        read_exact(connection, padding)
        if kind == 6:
            chunks.append(body)
        if kind == 3:
            if body != b'\0' * 8:
                raise AssertionError('Fin FastCGI invalide')
            return json.loads(b''.join(chunks).split(b'\r\n\r\n', 1)[1])


class FastCGIFixtureTests(unittest.TestCase):
    def test_healthy_status_has_complete_records_and_expected_pool_counts(self):
        with fake_status_server() as server:
            with socket.create_connection(server.server_address, timeout=1) as connection:
                request(connection)
                document = response(connection)
                self.assertEqual(document['pool'], 'www')
                self.assertEqual(document['active processes'], 1)
                self.assertEqual(document['total processes'], 2)
                self.assertEqual(len(document['processes']), 2)

    def test_blocked_reader_does_not_prevent_a_healthy_following_connection(self):
        with fake_status_server() as server:
            server.mode = 'stall'
            with socket.create_connection(server.server_address, timeout=0.2) as blocked:
                request(blocked)
                with self.assertRaises(socket.timeout):
                    blocked.recv(1)
                server.mode = 'healthy'
                with socket.create_connection(server.server_address, timeout=1) as healthy:
                    request(healthy)
                    self.assertEqual(response(healthy)['total processes'], 2)

    def test_partial_header_is_followed_by_a_blocked_read(self):
        with fake_status_server() as server:
            server.mode = 'partial'
            with socket.create_connection(server.server_address, timeout=0.2) as connection:
                request(connection)
                self.assertEqual(read_exact(connection, 4), b'\1\6\0\1')
                with self.assertRaises(socket.timeout):
                    connection.recv(1)


if __name__ == '__main__':
    unittest.main()
