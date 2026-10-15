#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(git rev-parse --show-toplevel)"
cd "$repo_dir"

if [[ ! -x .venv/bin/python ]]; then
  python3.12 -m venv .venv
fi
requirements_fingerprint="$(sha256sum control-plane/requirements-lock.txt diagnostics/requirements-lock.txt ml/requirements.txt scripts/codespaces/requirements.txt | sha256sum | cut -d' ' -f1)"
python_stamp=.venv/.codespaces-requirements.sha256
if [[ ! -f "$python_stamp" ]] || [[ "$(<"$python_stamp")" != "$requirements_fingerprint" ]]; then
  .venv/bin/python -m pip install --disable-pip-version-check --only-binary=:all: \
    --timeout 120 --retries 5 \
    -r control-plane/requirements-lock.txt \
    -r diagnostics/requirements-lock.txt \
    -r ml/requirements.txt \
    -r scripts/codespaces/requirements.txt
  printf '%s\n' "$requirements_fingerprint" > "$python_stamp"
fi

collection_fingerprint="$(sha256sum ansible/requirements.yml | cut -d' ' -f1)"
collection_stamp=.venv/.codespaces-collections.sha256
collection_root=.venv/ansible-collections
docker_collection_manifest="$collection_root/ansible_collections/community/docker/MANIFEST.json"
if [[ ! -f "$collection_stamp" ]] || [[ "$(<"$collection_stamp")" != "$collection_fingerprint" ]] || \
   [[ ! -f "$docker_collection_manifest" ]]; then
  mkdir -p "$collection_root"
  .venv/bin/ansible-galaxy collection install \
    --collections-path "$collection_root" \
    -r ansible/requirements.yml
  if [[ ! -f "$docker_collection_manifest" ]]; then
    echo "La collection Ansible community.docker n'a pas ete installee correctement." >&2
    exit 2
  fi
  printf '%s\n' "$collection_fingerprint" > "$collection_stamp"
fi

dashboard_fingerprint="$(sha256sum dashboard/package-lock.json | cut -d' ' -f1)"
dashboard_stamp=dashboard/node_modules/.codespaces-package-lock.sha256
if [[ ! -f "$dashboard_stamp" ]] || [[ "$(<"$dashboard_stamp")" != "$dashboard_fingerprint" ]]; then
  npm --prefix dashboard ci --no-audit --no-fund
  printf '%s\n' "$dashboard_fingerprint" > "$dashboard_stamp"
fi

echo "Environnement prêt. Déploiement volontairement non lancé."
echo "Commande suivante : bash scripts/codespaces/deploy.sh"
