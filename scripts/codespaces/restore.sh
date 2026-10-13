#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
(( EUID == 0 )) || { echo 'Executer avec sudo' >&2; exit 1; }
[[ $# == 2 && $1 =~ ^[0-9a-f]{64}$ && $2 == --confirmer-restauration ]] || { echo 'Usage: sudo restore.sh ID_COMPLET --confirmer-restauration' >&2; exit 1; }
SNAPSHOT=$1
STORE=/opt/moodle-pfe-backup
BASE=/opt/moodle
RESTIC=/workspaces/moodle-infra/scripts/codespaces/restic.sh
DC=(docker compose -p moodle-pilote -f "$BASE/compose.yml")
[[ -x "$RESTIC" && -f "$BASE/compose.yml" ]] || { echo 'Configuration manquante' >&2; exit 1; }
mkdir -p "$STORE/staging" "$STORE/rollback"
exec 9>/run/lock/moodle-pilote-backup.lock
flock -n 9 || { echo 'Sauvegarde ou restauration deja en cours' >&2; exit 1; }
"$RESTIC" snapshots --json --tag moodle-pilote-full | python3 -c 'import json,sys; s=sys.argv[1]; x=json.load(sys.stdin); sys.exit(0 if any(i.get("id")==s for i in x) else 1)' "$SNAPSHOT" || { echo 'Snapshot Moodle introuvable' >&2; exit 1; }
free_kb=$(df -Pk "$STORE" | awk 'NR==2 {print $4}')
(( free_kb > 3*1024*1024 )) || { echo 'Espace libre insuffisant (minimum 3 Gio)' >&2; exit 1; }
stage=$(mktemp -d "$STORE/staging/restore-XXXXXXXX")
rollback=''
frozen=0
replaced=0
cleanup() {
  rc=$?
  trap - EXIT INT TERM
  set +e
  if (( rc != 0 && replaced )) && [[ -n "$rollback" ]]; then
    echo 'Echec : tentative de retour aux donnees anterieures' >&2
    find "$DATA" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    tar -C "$DATA" -xpf "$rollback/moodledata-before.tar" || rc=1
    "${DC[@]}" exec -T --user postgres postgres sh -ec 'export PGPASSWORD="$(cat /run/secrets/postgres_admin_password)"; exec pg_restore -h 127.0.0.1 -U postgres -d moodle --clean --if-exists --single-transaction --exit-on-error' < "$rollback/moodle-before.pgdump" || rc=1
  fi
  if (( frozen )); then "${DC[@]}" up -d --no-recreate --wait --wait-timeout 180 php apache || rc=1; fi
  rm -rf -- "$stage"
  if (( rc != 0 )); then echo "Restauration en echec. Copie de securite : $rollback" >&2; fi
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$RESTIC" restore "$SNAPSHOT" --target "$stage"
mapfile -t archives < <(find "$stage/opt/moodle-pfe-backup/staging" -mindepth 1 -maxdepth 1 -type d -name 'run-*')
[[ ${#archives[@]} == 1 ]] || { echo 'Structure du snapshot inattendue' >&2; exit 1; }
archive=${archives[0]}
(cd "$archive" && sha256sum -c SHA256SUMS)
for f in moodledata.tar code.tar configuration.tar; do tar -tf "$archive/$f" >/dev/null; done
[[ $(docker volume inspect -f '{{.Driver}}' moodle-pilote_moodledata) == local ]] || exit 1
DATA=$(docker volume inspect -f '{{.Mountpoint}}' moodle-pilote_moodledata)
[[ -d "$DATA" && $DATA == /* ]] || exit 1
exec 8>/run/lock/moodle-pilote-cron.lock
flock -w 60 8 || { echo 'Cron Moodle occupe' >&2; exit 1; }
rollback=$(mktemp -d "$STORE/rollback/pre-restore-XXXXXXXX")
frozen=1
"${DC[@]}" stop --timeout 60 apache
"${DC[@]}" stop --timeout 60 php
"${DC[@]}" exec -T --user postgres postgres sh -ec 'export PGPASSWORD="$(cat /run/secrets/postgres_admin_password)"; exec pg_dump -h 127.0.0.1 -U postgres -d moodle -Fc' > "$rollback/moodle-before.pgdump"
[[ -s "$rollback/moodle-before.pgdump" ]] || exit 1
tar -C "$DATA" -cpf "$rollback/moodledata-before.tar" .
replaced=1
find "$DATA" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
tar -C "$DATA" -xpf "$archive/moodledata.tar"
"${DC[@]}" exec -T --user postgres postgres sh -ec 'export PGPASSWORD="$(cat /run/secrets/postgres_admin_password)"; exec pg_restore -h 127.0.0.1 -U postgres -d moodle --clean --if-exists --single-transaction --exit-on-error' < "$archive/moodle.pgdump"
"${DC[@]}" up -d --no-recreate --wait --wait-timeout 180 php apache
frozen=0
replaced=0
echo "Restauration des donnees Moodle terminee. Copie prealable : $rollback"
