#!/usr/bin/env bash
set -euo pipefail

sudo mkdir -p /var/log/supervisor
if ! sudo supervisorctl pid >/dev/null 2>&1; then
  sudo supervisord -c /etc/supervisor/supervisord.conf
fi
