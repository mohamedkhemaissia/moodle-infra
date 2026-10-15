#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
(( EUID == 0 )) || { echo "Lancer avec sudo" >&2; exit 1; }
BASE=/opt/moodle
STORE=/opt/moodle-pfe-backup
CODE="${MOODLE_CODE_DIR:-$(find "$BASE/releases" -mindepth 2 -maxdepth 2 -type d -name moodle -print -quit)}"
RESTIC="${MOODLE_RESTIC_WRAPPER:-/usr/local/sbin/moodle-site-restic}"
[[ -x "$RESTIC" ]] || RESTIC=/workspaces/moodle-infra/scripts/codespaces/restic.sh
RECORD_SUCCESS="${MOODLE_RECORD_SUCCESS:-/usr/local/lib/moodle-site-backup/record-success.py}"
[[ -f "$RECORD_SUCCESS" ]] || RECORD_SUCCESS="$(dirname "$0")/record-success.py"
DC=(docker compose -p moodle-pilote -f "$BASE/compose.yml")
mkdir -p "$STORE/staging"
chmod 700 "$STORE/staging"
exec 9>/run/lock/moodle-pilote-backup.lock
flock -n 9 || { echo "Une sauvegarde est deja en cours" >&2; exit 1; }
[[ -d "$CODE" && -f "$BASE/compose.yml" && -x "$RESTIC" ]] || { echo "Fichiers requis absents" >&2; exit 1; }
"$RESTIC" cat config >/dev/null
DATA=$(docker volume inspect -f "{{.Mountpoint}}" moodle-pilote_moodledata)
[[ -d "$DATA" ]] || exit 1
stage=$(mktemp -d "$STORE/staging/run-XXXXXXXX")
frozen=0
cron_was_running=0
if "${DC[@]}" --profile moodle-cron ps --status running --services | grep -qx cron; then
  cron_was_running=1
fi
cleanup() {
  rc=$?
  trap - EXIT INT TERM
  if (( frozen )); then
    "${DC[@]}" up -d --no-recreate --wait --wait-timeout 180 php apache || rc=1
    if (( cron_was_running )); then
      "${DC[@]}" --profile moodle-cron up -d --no-recreate --wait --wait-timeout 240 cron || rc=1
    fi
  fi
  if [[ "$stage" == "$STORE/staging/"run-* ]]; then rm -rf -- "$stage" || rc=1; fi
  if [[ -n "${result_file:-}" ]]; then rm -f -- "$result_file"; fi
  exit "$rc"
}
trap cleanup EXIT
trap "exit 130" INT
trap "exit 143" TERM
frozen=1
if (( cron_was_running )); then "${DC[@]}" --profile moodle-cron stop --timeout 60 cron; fi
"${DC[@]}" stop --timeout 60 apache
"${DC[@]}" stop --timeout 60 php
frozen_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
"${DC[@]}" exec -T --user postgres postgres sh -c "PGPASSWORD=\$(cat /run/secrets/postgres_admin_password) pg_dump -h 127.0.0.1 -U postgres -Fc -d moodle" > "$stage/moodle.pgdump"
[[ -s "$stage/moodle.pgdump" ]] || exit 1
"${DC[@]}" exec -T --user postgres postgres pg_restore -l < "$stage/moodle.pgdump" >/dev/null
tar -C "$DATA" -cf "$stage/moodledata.tar" .
tar -C "$CODE" -cf "$stage/code.tar" .
paths=(compose.yml config secrets tls)
[[ ! -d "$BASE/build" ]] || paths+=(build)
tar -C "$BASE" -cf "$stage/configuration.tar" "${paths[@]}"
printf "frozen_at_utc=%s\nproject=moodle-pilote\n" "$frozen_at" > "$stage/manifest.txt"
(cd "$stage" && sha256sum moodle.pgdump moodledata.tar code.tar configuration.tar manifest.txt > SHA256SUMS)
"${DC[@]}" up -d --no-recreate --wait --wait-timeout 180 php apache
if (( cron_was_running )); then "${DC[@]}" --profile moodle-cron up -d --no-recreate --wait --wait-timeout 240 cron; fi
frozen=0
result_file=$(mktemp "$STORE/restic-result.XXXXXXXX")
"$RESTIC" backup --json --tag moodle-pilote-full "$stage" > "$result_file"
"$RESTIC" check --read-data-subset=5% >/dev/null
python3 "$RECORD_SUCCESS" "$result_file" "$frozen_at" "$BASE/backups/last-success.txt"
echo "Sauvegarde Moodle chiffree terminee"
