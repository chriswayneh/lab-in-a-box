#!/bin/sh
# Compose file-backed secrets retain host ownership. Copy this private secret
# into Grafana's existing data volume so UID 472 can read it without making
# the host file public or putting the password in the process environment.
set -eu

secret_dir=/var/lib/grafana/.secrets
temporary="$secret_dir/admin_password.tmp"
mkdir -p "$secret_dir"
chmod 700 "$secret_dir"
chown 472:472 "$secret_dir"
umask 077
trap 'rm -f "$temporary"' EXIT
cat /run/secrets/grafana_admin_password > "$temporary"
chown 472:472 "$temporary"
chmod 400 "$temporary"
mv "$temporary" "$secret_dir/admin_password"
trap - EXIT
printf '%s\n' 'Grafana private password file ready.'
