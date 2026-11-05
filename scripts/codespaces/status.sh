#!/usr/bin/env bash
set -euo pipefail

failures=0

record_failure() {
  local label="$1"
  local detail="$2"

  printf '%s: ECHEC (%s)\n' "$label" "$detail"
  failures=$((failures + 1))
}

http_code() {
  local code

  code="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --connect-timeout 3 --max-time 10 --max-redirs 0 "$@")" || code="000"
  printf '%s' "$code"
}

check_http() {
  local label="$1"
  local url="$2"
  local expected="${3:-200}"
  local code

  code="$(http_code "$url")"
  if [[ "$code" == "$expected" ]]; then
    printf '%s: OK (HTTP %s)\n' "$label" "$code"
  else
    record_failure "$label" "HTTP $code ; HTTP $expected attendu"
  fi
}

check_worker() {
  local label="$1"
  local required="$2"
  shift 2
  local output

  if output="$("$@" 2>&1)"; then
    output="${output//$'\n'/; }"
    if [[ -n "$output" ]]; then
      printf '%s: disponible (%s)\n' "$label" "$output"
    else
      printf '%s: disponible\n' "$label"
    fi
  elif [[ "$required" == "required" ]]; then
    record_failure "$label" "healthcheck indisponible"
  else
    printf '%s: indisponible (informatif)\n' "$label"
  fi
}

printf '%s\n' '=== Socle du pilote Moodle ==='
check_http "API de pilotage" "http://127.0.0.1:8000/api/v1/health"
check_http "Tableau de bord" "http://127.0.0.1:8088/"

moodle_code="$(http_code "http://127.0.0.1:8443/login/index.php")"
if [[ "$moodle_code" == "200" ]]; then
  printf 'Moodle derriere le proxy: OK (HTTP 200)\n'
elif [[ "$moodle_code" =~ ^3[0-9][0-9]$ ]]; then
  record_failure "Moodle derriere le proxy" \
    "redirection HTTP $moodle_code refusee ; HTTP 200 attendu"
else
  record_failure "Moodle derriere le proxy" "HTTP $moodle_code ; HTTP 200 attendu"
fi

check_http "Prometheus" "http://127.0.0.1:9090/-/ready"
check_http "Alertmanager" "http://127.0.0.1:9093/-/ready"

dashboard_origin="https://${CODESPACE_NAME}-8088.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
origin_code="$(http_code --request POST http://127.0.0.1:8088/api/v1/auth/login \
  --header "Origin: $dashboard_origin" \
  --header 'Content-Type: application/json' \
  --data '{}')"
if [[ "$origin_code" == "422" ]]; then
  printf 'Origine dashboard: acceptee (%s)\n' "$dashboard_origin"
else
  record_failure "Origine dashboard" \
    "HTTP $origin_code ; HTTP 422 attendu pour la requete de validation"
fi

printf '%s\n' '=== Workers et traitements ==='
check_worker "Worker diagnostics" required \
  sudo /usr/local/sbin/moodle-diagnostic-requests-worker --healthcheck
check_worker "Worker interventions" required \
  sudo /usr/local/sbin/moodle-remediation-worker --healthcheck
check_worker "Profils PHP-FPM" required \
  sudo /usr/local/sbin/moodle-fpm-profiles --status
if site_worker_status="$(sudo /usr/local/sbin/moodle-site-operations-worker \
  --healthcheck 2>&1)"; then
  site_worker_status="${site_worker_status//$'\n'/; }"
  printf 'Sauvegarde complete: disponible (worker %s)\n' "$site_worker_status"
else
  printf '%s\n' 'Sauvegarde complete: indisponible (informatif)'
fi

if sudo docker compose -p moodle-pilote -f /opt/moodle/compose.yml \
  --profile moodle-cron exec -T cron \
  sh -ec 'last=$(cat /tmp/moodle-cron-last-success 2>/dev/null || echo 0); now=$(date +%s); [ $((now - last)) -le 180 ]' \
  >/dev/null 2>&1; then
  printf '%s\n' 'Cron Moodle: actif (execution reussie dans les 3 dernieres minutes)'
else
  record_failure "Cron Moodle" "inactif, absent ou derniere execution en echec"
fi

if sudo docker compose -p moodle-pilote -f /opt/moodle/compose.yml \
  --profile course-operations exec -T course-operations \
  php /opt/moodle-course-operations/worker.php --healthcheck \
  >/dev/null 2>&1; then
  printf '%s\n' \
    'Worker sauvegarde/restauration de cours: disponible (fichier .mbz et nouveau cours)'
else
  record_failure "Worker sauvegarde/restauration de cours" "healthcheck indisponible"
fi

printf '%s\n' '=== Processus Supervisor ==='
if supervisor_status="$(sudo supervisorctl status \
  moodle-container-metrics \
  moodle-diagnostic-requests \
  moodle-remediation-worker \
  moodle-site-operations 2>&1)"; then
  printf '%s\n' "$supervisor_status"
else
  printf '%s\n' "$supervisor_status"
  record_failure "Processus Supervisor" "un ou plusieurs processus ne sont pas disponibles"
fi

email_settings="${XDG_CONFIG_HOME:-$HOME/.config}/moodle-pilotage/email.json"
if [[ -f "$email_settings" ]]; then
  printf '%s\n' \
    'Relais e-mail: configure (informatif ; utiliser scripts/codespaces/test-email.sh pour la recette)'
else
  printf '%s\n' 'Relais e-mail: non configure (informatif)'
fi

if ((failures > 0)); then
  printf '\nBilan: ECHEC (%d controle(s) obligatoire(s) en echec)\n' "$failures"
  exit 1
fi

printf '\n%s\n' 'Bilan: OK (socle disponible)'
