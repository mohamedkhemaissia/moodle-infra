"""Sonde HTTPS bornee, connexion loopback avec verification du nom Moodle."""
import socket
import ssl
import time
from html.parser import HTMLParser
from http.client import HTTPConnection, HTTPException


class LoginFields(HTMLParser):
    def __init__(self):
        super().__init__()
        self.names = set()

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            self.names.add(dict(attrs).get("name"))


class RoutedTLS(HTTPConnection):
    def __init__(self, hostname, port, context, deadline):
        super().__init__(hostname, port, timeout=5)
        self.context, self.deadline = context, deadline

    def remaining(self):
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("Delai total depasse.")
        return value

    def connect(self):
        raw = socket.create_connection(("127.0.0.1", self.port), timeout=self.remaining())
        try:
            raw.settimeout(self.remaining())
            self.sock = self.context.wrap_socket(raw, server_hostname=self.host)
            self.sock.settimeout(self.remaining())
        except BaseException:
            raw.close()
            raise


def login_probe(hostname, port, ca_file):
    start = time.monotonic()
    result = {"success": False, "http_status": None, "tls_validated": False, "error": None}
    connection = None
    try:
        context = ssl.create_default_context(cafile=str(ca_file))
        connection = RoutedTLS(hostname, port, context, start + 5)
        connection.request("GET", "/login/index.php", headers={"Accept": "text/html", "Connection": "close", "Accept-Encoding": "identity"})
        result["tls_validated"] = True
        active_socket = connection.sock
        active_socket.settimeout(connection.remaining())
        with connection.getresponse() as response:
            result["http_status"] = response.status
            if response.status != 200:
                result["error"] = "unexpected_http_status"
            elif "text/html" not in response.getheader("Content-Type", "").lower():
                result["error"] = "unexpected_content_type"
            else:
                body, size = [], 0
                length = response.getheader("Content-Length")
                expected = int(length) if length is not None and length.isdigit() else None
                while not response.isclosed():
                    active_socket.settimeout(connection.remaining())
                    chunk = response.read1(min(65536, 524289 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 524288:
                        result["error"] = "body_too_large"
                        break
                    body.append(chunk)
                if result["error"] is None and expected is not None and size != expected:
                    result["error"] = "incomplete_body"
                if result["error"] is None:
                    parser = LoginFields()
                    parser.feed(b"".join(body).decode("utf-8", errors="replace"))
                    if not {"username", "password"}.issubset(parser.names):
                        result["error"] = "login_form_missing"
        result["success"] = result["error"] is None
    except ssl.SSLCertVerificationError:
        result["error"] = "tls_verification_failed"
    except ssl.SSLError:
        result["error"] = "tls_failed"
    except TimeoutError:
        result["error"] = "timeout"
    except HTTPException:
        result["error"] = "http_protocol_error"
    except (OSError, ValueError):
        result["error"] = "probe_unavailable"
    finally:
        if connection:
            connection.close()
    result["duration_ms"] = round((time.monotonic() - start) * 1000, 3)
    return result
