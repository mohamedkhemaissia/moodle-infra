"""Secours borne pour le scenario de validation PHP dans Codespaces."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


RUNTIME_DIR = Path("/run/moodle-remediation-validation")


def confined_marker(value):
    marker = Path(value)
    if not marker.is_absolute() or marker.parent != RUNTIME_DIR or not re.fullmatch(
        r"pfe-auto-php-secours-[0-9a-f]{12}\.json", marker.name
    ):
        raise ValueError("Marqueur de secours invalide.")
    return marker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--delay", required=True, type=int)
    parser.add_argument("--marker", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Le secours doit etre lance par root.")
    if not re.fullmatch(r"[0-9a-f]{64}", args.container_id) or not 60 <= args.delay <= 600:
        parser.error("Parametres de secours invalides.")
    try:
        marker = confined_marker(args.marker)
    except ValueError as exc:
        parser.error(str(exc))
    RUNTIME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    time.sleep(args.delay)
    started_at = time.time()
    result = subprocess.run(
        ["/usr/bin/docker", "start", args.container_id], capture_output=True, text=True,
        timeout=30, check=False, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
    )
    document = {"attempted_at": started_at, "container_id": args.container_id,
                "returncode": result.returncode, "started": result.returncode == 0}
    temporary = marker.with_suffix(".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(document, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    temporary.replace(marker)
    return 0 if result.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
