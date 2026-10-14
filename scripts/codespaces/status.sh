#!/usr/bin/env bash
set -euo pipefail

api_status="$(curl --silent --show-error --fail http://127.0.0.1:8000/api/v1/health)"
dashboard_status="$(curl --silent --show-error --fail --output /dev/null --write-out '%{http_code}' http://127.0.0.1:8088/)"
moodle_status="$(curl --insecure --silent --show-error --fail --output /dev/null --write-out '%{http_code}' https://127.0.0.1:443/login/index.php)"
worker_status="$(sudo /usr/local/sbin/moodle-diagnostic-requests-worker --healthcheck)"
dashboard_origin="https://${CODESPACE_NAME}-8088.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
origin_status="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  --request POST http://127.0.0.1:8088/api/v1/auth/login \
  --header "Origin: $dashboard_origin" --header 'Content-Type: application/json' --data '{}')"
if [[ "$origin_status" != "422" ]]; then
  echo "Origine dashboard refusee : $dashboard_origin (HTTP $origin_status)" >&2
  exit 1
fi

printf 'API: %s\n' "$api_status"
printf 'Dashboard HTTP: %s\n' "$dashboard_status"
printf 'Origine dashboard: acceptee\n'
printf 'Moodle HTTPS: %s\n' "$moodle_status"
printf 'File diagnostics: %s\n' "$worker_status"
sudo supervisorctl status moodle-container-metrics moodle-diagnostic-requests
