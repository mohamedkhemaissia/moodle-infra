#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(git rev-parse --show-toplevel)"
settings_file="${XDG_CONFIG_HOME:-$HOME/.config}/moodle-pilotage/email.json"
if [[ ! -f "$settings_file" ]]; then
  echo "Configurer d'abord le relais : bash scripts/codespaces/configure-email.sh" >&2
  exit 2
fi
curl --silent --show-error --fail http://127.0.0.1:9093/-/ready >/dev/null

payload="$("$repo_dir/.venv/bin/python" - <<'PY'
from datetime import datetime, timedelta, timezone
import json
now = datetime.now(timezone.utc)
iso = lambda value: value.isoformat().replace("+00:00", "Z")
print(json.dumps([{
    "labels": {"alertname": "MoodlePilotEmailTest", "severity": "warning",
               "scope": "moodle-pilote", "environment": "pfe"},
    "annotations": {"summary": "Test de notification du pilote Moodle",
                    "description": "Aucune panne : message de validation demande par l'administrateur."},
    "startsAt": iso(now), "endsAt": iso(now + timedelta(minutes=2)),
    "generatorURL": "http://127.0.0.1:9090"
}], ensure_ascii=True))
PY
)"

curl --silent --show-error --fail \
  --header 'Content-Type: application/json' \
  --data "$payload" \
  http://127.0.0.1:9093/api/v2/alerts >/dev/null

echo "Alerte de test acceptee par Alertmanager. Verifier la boite de reception dans environ 30 secondes."
