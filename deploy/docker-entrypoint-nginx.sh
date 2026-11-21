#!/bin/sh
set -eu
if [ -z "${BASIC_AUTH_USER:-}" ] || [ -z "${BASIC_AUTH_PASS:-}" ]; then
  echo "ERROR: BASIC_AUTH_USER and BASIC_AUTH_PASS must be set" >&2
  exit 1
fi
printf '%s\n' "$BASIC_AUTH_PASS" | htpasswd -i -m -c /etc/nginx/.htpasswd "$BASIC_AUTH_USER"
chmod 600 /etc/nginx/.htpasswd
exec nginx -g "daemon off;"
