#!/usr/bin/env bash
set -euo pipefail

api_status="$(curl --silent --show-error --fail http://127.0.0.1:8000/api/v1/health)"
dashboard_status="$(curl --silent --show-error --fail --output /dev/null --write-out '%{http_code}' http://127.0.0.1:8088/)"
moodle_status="$(curl --insecure --silent --show-error --fail --output /dev/null --write-out '%{http_code}' https://127.0.0.1:443/login/index.php)"
worker_status="$(sudo /usr/local/sbin/moodle-diagnostic-requests-worker --healthcheck)"

printf 'API: %s\n' "$api_status"
printf 'Dashboard HTTP: %s\n' "$dashboard_status"
printf 'Moodle HTTPS: %s\n' "$moodle_status"
printf 'File diagnostics: %s\n' "$worker_status"
sudo supervisorctl status moodle-container-metrics moodle-diagnostic-requests
