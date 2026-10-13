#!/bin/sh
set -eu
exec /usr/bin/restic --repo /opt/moodle-pfe-backup/repository --password-file /etc/moodle-pfe-backup/restic-password "$@"
