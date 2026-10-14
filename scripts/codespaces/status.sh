#!/usr/bin/env bash
set -euo pipefail

api_status="$(curl --silent --show-error --fail http://127.0.0.1:8000/api/v1/health)"
dashboard_status="$(curl --silent --show-error --fail --output /dev/null --write-out '%{http_code}' http://127.0.0.1:8088/)"
moodle_status="$(curl --silent --show-error --fail --output /dev/null --write-out '%{http_code}' http://127.0.0.1:8443/login/index.php)"
worker_status="$(sudo /usr/local/sbin/moodle-diagnostic-requests-worker --healthcheck)"
remediation_status="$(sudo /usr/local/sbin/moodle-remediation-worker --healthcheck)"
php_profiles_status="$(sudo /usr/local/sbin/moodle-fpm-profiles --status)"
alertmanager_status="$(curl --silent --show-error --fail --output /dev/null --write-out '%{http_code}' http://127.0.0.1:9093/-/ready)"
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
printf 'Remediation automatique: %s\n' "$remediation_status"
printf 'Profils PHP-FPM: %s\n' "$php_profiles_status"
if sudo docker compose -p moodle-pilote -f /opt/moodle/compose.yml --profile course-operations exec -T course-operations \
  php /opt/moodle-course-operations/worker.php --healthcheck >/dev/null 2>&1; then
  printf '%s\n' 'Sauvegardes de cours: disponibles (fichier .mbz et restauration en nouveau cours)'
else
  printf '%s\n' 'Sauvegardes de cours: indisponibles'
fi
printf 'Alertmanager: HTTP %s\n' "$alertmanager_status"
if [[ -f "${XDG_CONFIG_HOME:-$HOME/.config}/moodle-pilotage/email.json" ]]; then
  printf 'Notifications email: configurees (utiliser scripts/codespaces/test-email.sh pour la recette)\n'
else
  printf 'Notifications email: non configurees\n'
fi
sudo supervisorctl status moodle-container-metrics moodle-diagnostic-requests moodle-remediation-worker
