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

write_cidr_file() {
  csv="$1"
  target="$2"
  directive="$3"
  trailer="$4"
  : > "$target"
  [ -n "$csv" ] || return 0
  set -f
  old_ifs="$IFS"
  IFS=','
  set -- $csv
  IFS="$old_ifs"
  has_entry=0
  for raw_cidr in "$@"; do
    cidr=$(printf '%s' "$raw_cidr" | tr -d '[:space:]')
    [ -z "$cidr" ] && continue
    case "$cidr" in
      *[!0-9a-fA-F:./]*)
        echo "ERROR: Invalid CIDR entry ($cidr) for $target" >&2
        exit 1
        ;;
    esac
    printf '%s %s;\n' "$directive" "$cidr" >> "$target"
    has_entry=1
  done
  set +f
  if [ "$has_entry" -eq 1 ] && [ -n "$trailer" ]; then
    printf '%s\n' "$trailer" >> "$target"
  fi
}

write_cidr_file "${ALLOWED_CIDRS:-}" /etc/nginx/allowlist.conf allow "allow 127.0.0.1;
allow ::1;
deny all;"
write_cidr_file "${TRUSTED_PROXY_CIDRS:-}" /etc/nginx/realip.conf set_real_ip_from "real_ip_header X-Real-IP;"

nginx -t
exec nginx -g "daemon off;"
