#!/usr/bin/env bash
# Exercise only uniquely named disposable resources, never the running lab.
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"
require_docker
server_image=lab-in-a-box/minio:2025-10-15-source1
client_image=lab-in-a-box/mc:2025-08-13-source1
initial_image=${MINIO_COMPATIBILITY_IMAGE:-$server_image}
suffix="$(date +%s)-$$"
name="lab-minio-check-$suffix"
network="$name-network"
data="$name-data"
secret="$name-secret"
init_path="$LAB_ROOT/scripts/init-minio.sh"
if command -v cygpath >/dev/null 2>&1; then init_path="$(cygpath -w "$init_path")"; fi
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm "$data" "$secret" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
}
trap cleanup EXIT
for kind in server client; do
  image="$server_image"
  if [[ "$kind" == client ]]; then image="$client_image"; fi
  docker run --rm --network none --read-only --entrypoint sh "$image" -ec "
    test -s /usr/share/minio/LICENSE
    test -s /usr/share/minio/NOTICE.md
    test -s /usr/share/minio/inputs.json
    test -s /usr/share/minio/$kind-source.tar.gz
  "
done
test "$(docker image inspect "$server_image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = 9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a
test "$(docker image inspect "$client_image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = 7394ce0dd2a80935aded936b09fa12cbb3cb8096
docker run --rm --network none "$server_image" --version | grep -F 'RELEASE.2025-10-15T17-29-55Z'
docker run --rm --network none "$client_image" --version | grep -F 'go1.27.1'
docker run --rm --network none --entrypoint sh "$server_image" -ec 'echo "45521908307306e925c98d629e1c17d78c8b72b6ee242b1bfb1409f7d8ee5841  /usr/share/minio/server-source.tar.gz" | sha256sum -c -'
docker run --rm --network none --entrypoint sh "$client_image" -ec 'echo "95cd293c7119f16921a6dc515a1fb74a2227f19fd994b9c8b770a154e802ac44  /usr/share/minio/client-source.tar.gz" | sha256sum -c -'
docker network create "$network" >/dev/null
docker volume create "$data" >/dev/null
docker volume create "$secret" >/dev/null
docker run --rm --network none -v "$secret:/run/secrets" --entrypoint sh "$client_image" -ec \
  'printf "%s" "disposable-test-password" > /run/secrets/minio_root_password; chmod 600 /run/secrets/minio_root_password'
start_server() {
  docker run -d --name "$name" --network "$network" \
  -e MINIO_ROOT_USER=fixtureadmin -e MINIO_ROOT_PASSWORD_FILE=/run/secrets/minio_root_password \
  -e MINIO_KMS_SECRET_KEY=fixture-key:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= \
  -v "$secret:/run/secrets:ro" -v "$data:/data" \
  "$1" server /data --console-address :9001 >/dev/null
}
start_server "$initial_image"
healthy() {
  local attempt
  for ((attempt=0; attempt<60; attempt++)); do
    if docker run --rm --network "$network" --entrypoint wget "$client_image" -q -O /dev/null "http://$name:9000/minio/health/live"; then return 0; fi
    sleep 1
  done
  docker logs "$name" >&2
  return 1
}
healthy
initialize() {
  docker run --rm --network "$network" -e MINIO_ROOT_USER=fixtureadmin \
    -e "MINIO_ENDPOINT=http://$name:9000" \
    -v "$secret:/run/secrets:ro" -v "$init_path:/scripts/init-minio.sh:ro" \
    --entrypoint sh "$client_image" /scripts/init-minio.sh
}
mc() {
  docker run --rm -i --network "$network" \
    -e "MC_HOST_fixture=http://fixtureadmin:disposable-test-password@$name:9000" \
    "$client_image" "$@"
}
initialize
for bucket in lab-artifacts lab-backups lab-datasets loki-chunks; do mc stat "fixture/$bucket" >/dev/null; done
mc version info fixture/lab-backups | grep -i enabled
mc ilm rule ls fixture/lab-backups | grep '30'
mc admin policy info fixture lab-app >/dev/null
printf '%s' persistence-fixture | mc pipe fixture/lab-artifacts/persistence.txt >/dev/null
mc encrypt set sse-s3 fixture/lab-backups >/dev/null
printf '%s' encrypted-fixture | mc pipe fixture/lab-backups/encrypted.txt >/dev/null
printf '%s' unicode-fixture | mc pipe fixture/lab-artifacts/café.txt >/dev/null
initialize
test "$(mc cat fixture/lab-artifacts/persistence.txt)" = persistence-fixture
docker rm -f "$name" >/dev/null
start_server "$server_image"
healthy
test "$(mc cat fixture/lab-artifacts/persistence.txt)" = persistence-fixture
test "$(mc cat fixture/lab-backups/encrypted.txt)" = encrypted-fixture
test "$(mc cat fixture/lab-artifacts/café.txt)" = unicode-fixture
printf '%s\n' 'MinIO pinned inputs, private-secret provisioning, S3 write/read and restart persistence passed.'
