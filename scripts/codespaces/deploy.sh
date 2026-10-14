#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${CODESPACE_NAME:-}" ]] || [[ -z "${CODESPACE_VSCODE_FOLDER:-}" ]] || \
   [[ -z "${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-}" ]]; then
  echo "Cette commande est réservée au Codespace du projet." >&2
  exit 2
fi

repo_dir="$(git rev-parse --show-toplevel)"
cd "$repo_dir"
export MOODLE_REPOSITORY_DIR="$repo_dir"

bash scripts/codespaces/post-start.sh
for attempt in {1..30}; do
  if docker info >/dev/null 2>&1; then
    break
  fi
  if [[ "$attempt" -eq 30 ]]; then
    echo "Le daemon Docker du Codespace n'est pas prêt." >&2
    exit 2
  fi
  sleep 2
done

latest_directory() {
  local parent="$1" pattern="$2" required="$3"
  local candidate
  candidate="$(find "$parent" -mindepth 1 -maxdepth 1 -type d -name "$pattern" -printf '%T@ %p\n' 2>/dev/null \
    | sort -nr | cut -d' ' -f2- | while IFS= read -r path; do
        [[ -f "$path/$required" ]] && { printf '%s\n' "$path"; break; }
      done)"
  [[ -n "$candidate" ]] && realpath "$candidate"
}

if [[ -z "${MOODLE_FEATURES_DIR:-}" ]]; then
  MOODLE_FEATURES_DIR="$(latest_directory ml/data/features 'moodle_features_*' metadata.json || true)"
  export MOODLE_FEATURES_DIR
fi
if [[ -z "${MOODLE_ANOMALY_BUNDLE_DIR:-}" ]]; then
  MOODLE_ANOMALY_BUNDLE_DIR="$(latest_directory ml/data/models 'moodle_anomalies_*' model.json || true)"
  export MOODLE_ANOMALY_BUNDLE_DIR
fi
if [[ -z "${MOODLE_FORECAST_BUNDLE_DIR:-}" ]]; then
  MOODLE_FORECAST_BUNDLE_DIR="$(latest_directory ml/data/models 'moodle_forecasts_*' models.json || true)"
  export MOODLE_FORECAST_BUNDLE_DIR
fi

if [[ -z "$MOODLE_FEATURES_DIR" ]] || [[ -z "$MOODLE_ANOMALY_BUNDLE_DIR" ]] || [[ -z "$MOODLE_FORECAST_BUNDLE_DIR" ]]; then
  echo "Information : aucun jeu complet d'artefacts IA locaux détecté ; le socle sera déployé et l'IA restera explicitement indisponible."
fi

.venv/bin/ansible-playbook \
  -i ansible/inventories/codespaces.ini \
  ansible/playbooks/pilot-codespaces.yml

echo "Déploiement terminé. Crée maintenant l'unique compte administrateur :"
echo "  sudo moodle-pilotage-user add --username admin --name 'Administrateur PFE' --role admin"
echo "Tableau de bord : https://${CODESPACE_NAME}-8088.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
echo "Moodle : https://${CODESPACE_NAME}-443.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
