#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${CODESPACE_NAME:-}" ]] || [[ -z "${CODESPACE_VSCODE_FOLDER:-}" ]]; then
  echo "Cette commande est reservee au Codespace du projet." >&2
  exit 2
fi

repo_dir="$(git rev-parse --show-toplevel)"
cd "$repo_dir"

deploy=false
case "${1:-}" in
  "") ;;
  --deploy) deploy=true ;;
  *) echo "Usage : bash scripts/codespaces/train-ai.sh [--deploy]" >&2; exit 2 ;;
esac

if ! .venv/bin/python -c "import numpy, sklearn" >/dev/null 2>&1; then
  echo "Les dependances IA sont absentes ; executer d'abord : bash scripts/codespaces/setup.sh" >&2
  exit 2
fi
if ! curl --silent --show-error --fail http://127.0.0.1:9090/-/ready >/dev/null; then
  echo "Prometheus n'est pas pret. Verifier d'abord le deploiement du pilote." >&2
  exit 2
fi

lock_dir=.venv/.codespaces-ai-training.lock
if ! mkdir "$lock_dir" 2>/dev/null; then
  echo "Un entrainement Isolation Forest est deja en cours." >&2
  exit 2
fi
cleanup() {
  rmdir "$lock_dir" 2>/dev/null || true
}
trap cleanup EXIT

.venv/bin/python -B -m ml.train_live --hours 6

if [[ "$deploy" == true ]]; then
  bash scripts/codespaces/deploy.sh
else
  echo
  echo "Artefact Isolation Forest entraine et valide. Pour l'activer :"
  echo "  bash scripts/codespaces/deploy.sh"
fi
