#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${CODESPACE_NAME:-}" ]]; then
  echo "Cette commande est reservee au Codespace du projet." >&2
  exit 2
fi

repo_dir="$(git rev-parse --show-toplevel)"
python_bin="$repo_dir/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  echo "Executer d'abord : bash scripts/codespaces/setup.sh" >&2
  exit 2
fi

read -r -p "Relais SMTP (ex. smtp.gmail.com:587) : " smtp_smarthost
read -r -p "Adresse expediteur : " smtp_from
read -r -p "Nom utilisateur SMTP : " smtp_username
read -r -p "Adresse destinataire : " email_to
read -r -s -p "Mot de passe SMTP ou mot de passe d'application : " smtp_password
printf '\n'

settings_dir="${XDG_CONFIG_HOME:-$HOME/.config}/moodle-pilotage"
settings_file="$settings_dir/email.json"
mkdir -p "$settings_dir"
chmod 0700 "$settings_dir"
settings_tmp="$(mktemp "$settings_dir/email.json.tmp.XXXXXX")"
secret_tmp="$(mktemp "$settings_dir/smtp-password.tmp.XXXXXX")"
cleanup() {
  rm -f -- "$settings_tmp" "$secret_tmp"
}
trap cleanup EXIT

"$python_bin" - "$settings_tmp" "$smtp_smarthost" "$smtp_from" "$smtp_username" "$email_to" <<'PY'
import json
from pathlib import Path
import re
import sys

path, smarthost, sender, username, recipient = sys.argv[1:]
if not re.fullmatch(r"(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?):[1-9][0-9]{0,4}", smarthost):
    raise SystemExit("Relais SMTP invalide : utiliser hote:port.")
if int(smarthost.rsplit(":", 1)[1]) > 65535:
    raise SystemExit("Port SMTP invalide.")
email = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
if not email.fullmatch(sender) or not email.fullmatch(recipient):
    raise SystemExit("Adresse expediteur ou destinataire invalide.")
if not username or len(username) > 320 or any(c in username for c in "\r\n\0"):
    raise SystemExit("Nom utilisateur SMTP invalide.")
document = {"enabled": True, "smtp_smarthost": smarthost, "smtp_from": sender,
            "smtp_username": username, "email_to": recipient}
Path(path).write_text(json.dumps(document, ensure_ascii=True, sort_keys=True) + "\n")
Path(path).chmod(0o600)
PY

if [[ -z "$smtp_password" ]] || [[ ${#smtp_password} -gt 4096 ]] || [[ "$smtp_password" == *$'\n'* ]] || [[ "$smtp_password" == *$'\r'* ]]; then
  echo "Mot de passe SMTP invalide." >&2
  exit 2
fi
printf '%s' "$smtp_password" > "$secret_tmp"
unset smtp_password
chmod 0600 "$secret_tmp"

sudo install -d -o root -g 65534 -m 0750 /etc/moodle-alertmanager
sudo install -o root -g 65534 -m 0440 "$secret_tmp" /etc/moodle-alertmanager/smtp-password
mv -f -- "$settings_tmp" "$settings_file"
chmod 0600 "$settings_file"

echo "Configuration email enregistree hors du depot."
echo "Appliquer maintenant : bash scripts/codespaces/deploy.sh"
echo "Puis envoyer un test : bash scripts/codespaces/test-email.sh"
