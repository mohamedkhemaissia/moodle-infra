#!/bin/sh
set -eu

: "${DASHBOARD_PORT:=8088}"
: "${API_ORIGIN:=http://127.0.0.1:8000}"
export DASHBOARD_PORT API_ORIGIN

envsubst '${DASHBOARD_PORT} ${API_ORIGIN}' \
  < /etc/dashboard/nginx.conf.template > /tmp/nginx.conf

exec nginx -c /tmp/nginx.conf -g 'daemon off;'
