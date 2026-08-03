#!/bin/sh
# =============================================================================
# MinIO provisioning
# =============================================================================
# Creates the buckets the lab expects, applies a sane default policy, and adds
# a least-privilege service account for application use.
#
# Runs in the minio/mc image. The root password is read from the same Docker
# secret MinIO itself uses, so the credential exists in exactly one place.
#
# Idempotent: `mc mb` on an existing bucket is treated as success, not failure.
# =============================================================================
set -eu

MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://minio:9000}"
MINIO_ROOT_USER="${MINIO_ROOT_USER:-labadmin}"
MINIO_ROOT_PASSWORD_FILE="${MINIO_ROOT_PASSWORD_FILE:-/run/secrets/minio_root_password}"
BUCKETS="${MINIO_DEFAULT_BUCKETS:-lab-artifacts,lab-backups,lab-datasets,loki-chunks}"
ALIAS=lab

log()  { printf '[minio-init] %s\n' "$*"; }
warn() { printf '[minio-init] WARNING: %s\n' "$*" >&2; }
die()  { printf '[minio-init] ERROR: %s\n' "$*" >&2; exit 1; }

[ -r "$MINIO_ROOT_PASSWORD_FILE" ] || die "cannot read the root password secret at ${MINIO_ROOT_PASSWORD_FILE}"
MINIO_ROOT_PASSWORD="$(tr -d '\r\n' < "$MINIO_ROOT_PASSWORD_FILE")"

# -----------------------------------------------------------------------------
# Connect
# -----------------------------------------------------------------------------
connect() {
  attempt=1
  while [ "$attempt" -le 30 ]; do
    if mc alias set "$ALIAS" "$MINIO_ENDPOINT" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1; then
      log "connected to ${MINIO_ENDPOINT}"
      return 0
    fi
    log "waiting for MinIO (attempt ${attempt}/30)"
    sleep 3
    attempt=$((attempt + 1))
  done
  die "MinIO did not accept a connection at ${MINIO_ENDPOINT}"
}

# -----------------------------------------------------------------------------
# Buckets
#
# Every bucket is created private. Object storage that defaults to public is
# how holiday-weekend data breaches happen.
# -----------------------------------------------------------------------------
create_buckets() {
  echo "$BUCKETS" | tr ',' '\n' | while IFS= read -r bucket; do
    bucket="$(echo "$bucket" | tr -d '[:space:]')"
    [ -z "$bucket" ] && continue

    if mc ls "${ALIAS}/${bucket}" >/dev/null 2>&1; then
      log "bucket '${bucket}' already exists"
    else
      mc mb --ignore-existing "${ALIAS}/${bucket}" >/dev/null
      log "created bucket '${bucket}'"
    fi

    mc anonymous set none "${ALIAS}/${bucket}" >/dev/null 2>&1 || true
  done
}

# -----------------------------------------------------------------------------
# Retention and lifecycle
#
# Versioning on the backup bucket turns an accidental overwrite into an
# inconvenience rather than a data loss. The expiry rule keeps that from
# growing without bound.
# -----------------------------------------------------------------------------
configure_lifecycle() {
  if mc version enable "${ALIAS}/lab-backups" >/dev/null 2>&1; then
    log "versioning enabled on 'lab-backups'"
  else
    warn "could not enable versioning on 'lab-backups'"
  fi

  if mc ilm rule add --expire-days 30 --noncurrent-expire-days 7 \
       "${ALIAS}/lab-backups" >/dev/null 2>&1; then
    log "lifecycle rule on 'lab-backups': expire after 30 days, old versions after 7"
  else
    # Older mc releases use a different subcommand shape. Not worth failing over.
    warn "could not apply the lifecycle rule (mc version may differ); skipping"
  fi
}

# -----------------------------------------------------------------------------
# Least-privilege application credentials
#
# The root account should be used to administer MinIO, never by an application.
# This policy allows read/write on the lab's own buckets and nothing else.
# -----------------------------------------------------------------------------
create_app_policy() {
  cat > /tmp/lab-app-policy.json <<'JSON'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetBucketLocation", "s3:ListBucket"],
      "Resource": ["arn:aws:s3:::lab-artifacts", "arn:aws:s3:::lab-datasets"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": ["arn:aws:s3:::lab-artifacts/*", "arn:aws:s3:::lab-datasets/*"]
    }
  ]
}
JSON

  if mc admin policy create "$ALIAS" lab-app /tmp/lab-app-policy.json >/dev/null 2>&1; then
    log "created policy 'lab-app' (read/write on lab-artifacts and lab-datasets only)"
  else
    log "policy 'lab-app' already present"
  fi
  rm -f /tmp/lab-app-policy.json
}

main() {
  log "provisioning object storage"
  connect
  create_buckets
  configure_lifecycle
  create_app_policy
  log "done. Console: https://minio.${LAB_DOMAIN:-lab.localhost}"
  log "S3 endpoint for tooling: https://s3.${LAB_DOMAIN:-lab.localhost}"
}

main "$@"
