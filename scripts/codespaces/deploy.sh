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
# Le volume /workspaces de Codespaces est volontairement world-writable.
# Ansible n'y decouvre donc pas ansible.cfg automatiquement : les chemins
# absolus rendent le lancement independant de cette protection.
export ANSIBLE_CONFIG="$repo_dir/ansible.cfg"
export ANSIBLE_ROLES_PATH="$repo_dir/ansible/roles"
export ANSIBLE_COLLECTIONS_PATH="$repo_dir/.venv/ansible-collections"

if ! .venv/bin/python -c "import requests" >/dev/null 2>&1; then
  echo "Les dependances Codespaces ont change ; executer d'abord : bash scripts/codespaces/setup.sh" >&2
  exit 2
fi
if [[ ! -f "$ANSIBLE_COLLECTIONS_PATH/ansible_collections/community/docker/MANIFEST.json" ]]; then
  echo "La collection Ansible community.docker est absente ; executer d'abord : bash scripts/codespaces/setup.sh" >&2
  exit 2
fi

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

artifact_validation_dir="$(mktemp -d "${TMPDIR:-/tmp}/moodle-pilotage-artifacts.XXXXXX")"
cleanup_artifact_validation() {
  case "$artifact_validation_dir" in
    "${TMPDIR:-/tmp}"/moodle-pilotage-artifacts.*)
      rm -rf -- "$artifact_validation_dir"
      ;;
  esac
}
trap cleanup_artifact_validation EXIT

# Reproduire le contexte reel de l'image API. Il contient notamment la source
# partagee ml/isolation_model.py, absente volontairement de control-plane/app.
api_validation_context="$artifact_validation_dir/api-context"
.venv/bin/python -B ci/prepare_api.py \
  --output-dir "$api_validation_context" >/dev/null

validate_bundle() {
  local module="$1" bundle_dir="$2" features_dir="$3"
  local directory="$artifact_validation_dir/$module"
  mkdir -p "$directory"
  if [[ "$module" == "anomalies" ]]; then
    ln -sf "$bundle_dir/model.json" "$directory/model.json"
  else
    ln -sf "$bundle_dir/models.json" "$directory/models.json"
  fi
  ln -sf "$bundle_dir/report.json" "$directory/report.json"
  ln -sf "$features_dir/metadata.json" "$directory/features_metadata.json"
  PYTHONPATH="$api_validation_context" .venv/bin/python -B -m "app.$module" \
    --check-bundle "$directory" >/dev/null 2>&1
}

if [[ -n "$MOODLE_FEATURES_DIR" ]] && [[ -n "$MOODLE_ANOMALY_BUNDLE_DIR" ]] && \
   ! validate_bundle anomalies "$MOODLE_ANOMALY_BUNDLE_DIR" "$MOODLE_FEATURES_DIR"; then
  echo "Information : bundle d'anomalies local invalide ou incompatible ; detection IA desactivee."
  MOODLE_ANOMALY_BUNDLE_DIR=""
  export MOODLE_ANOMALY_BUNDLE_DIR
fi
if [[ -n "$MOODLE_FEATURES_DIR" ]] && [[ -n "$MOODLE_FORECAST_BUNDLE_DIR" ]] && \
   ! validate_bundle forecasts "$MOODLE_FORECAST_BUNDLE_DIR" "$MOODLE_FEATURES_DIR"; then
  echo "Information : bundle de previsions local invalide ou incompatible ; previsions IA desactivees."
  MOODLE_FORECAST_BUNDLE_DIR=""
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
echo "Moodle : https://${CODESPACE_NAME}-8443.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
if [[ -f "${XDG_CONFIG_HOME:-$HOME/.config}/moodle-pilotage/email.json" ]]; then
  echo "Notifications email : configurees ; recette avec bash scripts/codespaces/test-email.sh"
else
  echo "Notifications email : a configurer avec bash scripts/codespaces/configure-email.sh"
fi
