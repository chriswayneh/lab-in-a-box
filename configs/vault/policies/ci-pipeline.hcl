# =============================================================================
# ci-pipeline — machine identity for automated builds
# =============================================================================
# Bound to an AppRole rather than a human login. Scoped as narrowly as a build
# can tolerate: it reads the handful of secrets a deploy needs and can do
# nothing else. A leaked pipeline token should be boring.
# =============================================================================

path "secret/data/ci/*" {
  capabilities = ["read"]
}

path "secret/metadata/ci/*" {
  capabilities = ["list"]
}

# Registry and artifact-store credentials required to push a build.
path "secret/data/infrastructure/registry" {
  capabilities = ["read"]
}

path "secret/data/infrastructure/object-storage" {
  capabilities = ["read"]
}

# Renew and revoke its own token so a long build does not expire mid-run, and a
# finished build can hand its credential back immediately.
path "auth/token/renew-self" {
  capabilities = ["update"]
}

path "auth/token/revoke-self" {
  capabilities = ["update"]
}
