#!/bin/sh
set -eu

if [ -z "${BASIC_AUTH_USER:-}" ] || [ -z "${BASIC_AUTH_PASS:-}" ]; then
  echo "ERROR: BASIC_AUTH_USER and BASIC_AUTH_PASS must be set" >&2
  exit 1
fi

if [ "${#BASIC_AUTH_PASS}" -lt 8 ] || [ "$BASIC_AUTH_PASS" = "ChangeMe_StrongPass_2026" ]; then
  echo "ERROR: BASIC_AUTH_PASS must be at least 8 characters and not use the example placeholder" >&2
  exit 1
fi

printf '%s\n' "$BASIC_AUTH_PASS" | htpasswd -i -m -c /etc/nginx/.htpasswd "$BASIC_AUTH_USER"
chown root:nginx /etc/nginx/.htpasswd
chmod 640 /etc/nginx/.htpasswd

allowlist="/etc/nginx/allowlist.conf"
: > "$allowlist"
if [ -n "${ALLOWED_CIDRS:-}" ]; then
  set -f
  old_ifs="$IFS"
  IFS=','
  set -- $ALLOWED_CIDRS
  IFS="$old_ifs"
  has_entry=0
  for raw_cidr in "$@"; do
    cidr=$(printf '%s' "$raw_cidr" | tr -d '[:space:]')
    [ -z "$cidr" ] && continue
    case "$cidr" in
      *[!0-9a-fA-F:./]*)
        echo "ERROR: Invalid CIDR entry in ALLOWED_CIDRS: $cidr" >&2
        exit 1
        ;;
    esac
    printf 'allow %s;\n' "$cidr" >> "$allowlist"
    has_entry=1
  done
  set +f
  if [ "$has_entry" -eq 1 ]; then
    printf 'allow 127.0.0.1;\nallow ::1;\ndeny all;\n' >> "$allowlist"
  fi
fi

nginx -t
exec nginx -g "daemon off;"
