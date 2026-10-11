"""Identite des commandes locales ; le secret ne sort jamais de l'API de pilotage."""
import hashlib
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

READ_PATH = re.compile(
    r"^/api/v1/(metrics/(?:summary|applications|containers)|storage/health|backups/(?:(?:pilotage/)?summary|catalog)|"
    r"(forecasts/summary|anomalies/(summary|model))|"
    r"incidents(?:/summary|/[1-9][0-9]*(?:/events)?)?|"
    r"decisions/(?:summary|policies|history(?:/[1-9][0-9]*)?|[1-9][0-9]*)|policies|"
    r"(?:diagnostics|remediations)(?:/summary|/[0-9a-f]{32})?)$")


def read_secret_file(path):
    target = Path(path)
    if (not target.is_absolute() or target.is_symlink()
            or not target.is_file() or target.stat().st_mode & 0o007):
        raise ValueError("Fichier d'identite locale absent ou incompatible.")
    value = target.read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Identite locale invalide.")
    return value


def worker_headers(url):
    path = os.getenv("PILOTAGE_SERVICE_TOKEN_FILE")
    if path is None:
        path = "/etc/moodle-pilotage/worker-token" if Path("/etc/moodle-pilotage/auth-active").is_file() else ""
    if not path:
        return {}
    parsed = urlsplit(url)
    if not READ_PATH.fullmatch(parsed.path):
        return {}
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username or parsed.password or parsed.fragment or not parsed.port):
        raise ValueError("Le secret d'exploitation est reserve a l'API locale.")
    return {"Authorization": "Bearer " + read_secret_file(path)}


def token_fingerprint(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()
