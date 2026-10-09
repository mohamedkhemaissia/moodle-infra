"""Telecharger les images de CI avant les tests, avec trois tentatives reseau au plus."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys
import time

import yaml
from registry_sources import image_reference

ROOT = Path(__file__).resolve().parents[1]
MAX_ATTEMPTS = 3
ATTEMPT_TIMEOUT_SECONDS = 120
PERMANENT = re.compile(
    r"unauthorized|incorrect username|authentication required|access denied|denied:|"
    r"too ?many ?requests|toomanyrequests|pull rate limit|manifest unknown|not found|"
    r"invalid reference format|no matching manifest", re.IGNORECASE)
TRANSIENT = re.compile(
    r"deadline exceeded|Client\.Timeout|i/o timeout|TLS handshake timeout|"
    r"connection (?:reset|refused|timed out)|unexpected EOF|temporary failure|"
    r"no such host|request canceled|502 Bad Gateway|503 Service Unavailable|"
    r"504 Gateway Timeout", re.IGNORECASE)


def images_for(profile: str, root: Path = ROOT) -> list[str]:
    def defaults(role):
        return yaml.safe_load((root / 'ansible' / 'roles' / role / 'defaults' / 'main.yml').read_text())

    moodle = defaults('moodle')
    if profile == 'recovery-tests':
        images = [moodle['moodle_postgres_image'], moodle['moodle_apache_image'],
                  moodle['moodle_php_base_image']]
    elif profile == 'application-metrics':
        application = defaults('application_monitoring')
        monitoring = defaults('monitoring')
        images = [application['application_monitoring_php_image'],
                  application['application_monitoring_postgres_image'],
                  moodle['moodle_postgres_image'], moodle['moodle_php_base_image'],
                  monitoring['monitoring_prometheus_image']]
    else:
        raise ValueError('Profil CI inconnu.')
    if any(not isinstance(value, str) or not value or value.startswith('-') or
           any(character.isspace() for character in value) for value in images):
        raise ValueError('Reference image invalide dans les valeurs du projet.')
    return list(dict.fromkeys(image_reference(value) for value in images))


def output_text(value):
    if value is None:
        return ''
    return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value


def pull_image(image: str, *, run=subprocess.run, sleep=time.sleep):
    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f'Telechargement {image} : tentative {attempt}/{MAX_ATTEMPTS}', flush=True)
        try:
            result = run(['docker', 'pull', image], check=True, capture_output=True,
                         text=True, timeout=ATTEMPT_TIMEOUT_SECONDS)
            if result.stdout:
                print(output_text(result.stdout)[-8000:], flush=True)
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            output = '\n'.join(output_text(value) for value in (exc.stdout, exc.stderr))
            print(output[-8000:], file=sys.stderr, flush=True)
            # Ne jamais rejouer un test, un docker run ou une erreur permanente.
            retryable = (not PERMANENT.search(output) and
                         (isinstance(exc, subprocess.TimeoutExpired) or
                          TRANSIENT.search(output) is not None))
            if not retryable or attempt == MAX_ATTEMPTS:
                raise
            sleep(attempt * 5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', choices=['recovery-tests', 'application-metrics'])
    args = parser.parse_args()
    try:
        for image in images_for(args.profile):
            pull_image(image)
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print('::error::Images CI indisponibles ; les tests restent bloquants.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
