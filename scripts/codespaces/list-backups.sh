#!/usr/bin/env bash
set -euo pipefail
(( EUID == 0 )) || { echo "Executer avec sudo" >&2; exit 1; }
RESTIC=/workspaces/moodle-infra/scripts/codespaces/restic.sh
echo "=== SAUVEGARDES MOODLE DISPONIBLES ==="
"$RESTIC" snapshots --tag moodle-pilote-full
