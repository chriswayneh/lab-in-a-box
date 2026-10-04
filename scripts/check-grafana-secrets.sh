#!/usr/bin/env bash
# Prove a private source secret stays private while Grafana's UID can read
# the prepared copy. Named volumes give Linux file semantics on every host.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
require_docker

suffix="$(date +%s)-$$"
input_volume="lab-grafana-secret-input-${suffix}"
data_volume="lab-grafana-secret-data-${suffix}"
image=grafana/grafana:11.5.0
host_path="$LAB_ROOT/scripts/init-grafana-secrets.sh"
if command -v cygpath >/dev/null 2>&1; then host_path="$(cygpath -w "$host_path")"; fi
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

cleanup() {
  docker volume rm "$input_volume" "$data_volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker volume create "$input_volume" >/dev/null
docker volume create "$data_volume" >/dev/null

docker run --rm --network none --read-only --user 0:0 \
  -v "$input_volume:/run/secrets" --entrypoint /bin/sh "$image" -ec '
    printf "%s" "test-only-password" > /run/secrets/grafana_admin_password
    chmod 600 /run/secrets/grafana_admin_password
  '
docker run --rm --network none --read-only --user 472:472 \
  -v "$input_volume:/run/secrets:ro" --entrypoint /bin/sh "$image" -ec '
    test ! -r /run/secrets/grafana_admin_password
  '
docker run --rm --network none --read-only --user 0:0 \
  -v "$input_volume:/run/secrets:ro" -v "$data_volume:/var/lib/grafana" \
  -v "$host_path:/scripts/init-grafana-secrets.sh:ro" \
  --entrypoint /bin/sh "$image" /scripts/init-grafana-secrets.sh
docker run --rm --network none --read-only --user 472:472 \
  -v "$input_volume:/run/secrets:ro" -v "$data_volume:/var/lib/grafana:ro" \
  --entrypoint /bin/sh "$image" -ec '
    file=/var/lib/grafana/.secrets/admin_password
    test -r "$file"
    test "$(stat -c "%u:%g:%a" "$file")" = "472:472:400"
    test "$(stat -c "%u:%g:%a" /var/lib/grafana/.secrets)" = "472:472:700"
    test "$(wc -c < "$file")" -eq 18
    test ! -r /run/secrets/grafana_admin_password
  '
docker run --rm --network none --read-only --user 0:0 \
  -v "$input_volume:/run/secrets:ro" -v "$data_volume:/var/lib/grafana:ro" \
  --entrypoint /bin/sh "$image" -ec '
    cmp /run/secrets/grafana_admin_password /var/lib/grafana/.secrets/admin_password
    test "$(stat -c "%u:%g:%a" /run/secrets/grafana_admin_password)" = "0:0:600"
  '
printf '%s\n' 'Grafana generated-secret ownership and private-source regression passed.'
