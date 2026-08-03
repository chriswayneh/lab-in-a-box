# =============================================================================
# platform-admin — infrastructure operators
# =============================================================================
# Broad, but deliberately not root. The distinction matters: root can disable
# audit devices and erase its own tracks, which is precisely the capability an
# operator account should not have.
# =============================================================================

# Full control over the lab's application secrets.
path "secret/*" {
  capabilities = ["create", "read", "update", "delete", "list", "patch"]
}

# Manage encryption keys, but never export key material — the transit engine's
# whole value is that plaintext keys stay inside Vault.
path "transit/keys/*" {
  capabilities = ["create", "read", "update", "list"]
}

path "transit/export/*" {
  capabilities = ["deny"]
}

path "transit/encrypt/*" {
  capabilities = ["create", "update"]
}

path "transit/decrypt/*" {
  capabilities = ["create", "update"]
}

# Inspect and manage auth methods and policies.
path "sys/auth" {
  capabilities = ["read", "list"]
}

path "sys/policies/acl/*" {
  capabilities = ["create", "read", "update", "list"]
}

path "sys/mounts" {
  capabilities = ["read", "list"]
}

path "sys/health" {
  capabilities = ["read", "sudo"]
}

# Audit devices are explicitly off limits. Tampering with the audit trail
# requires the root token, which is checked in and out deliberately.
path "sys/audit" {
  capabilities = ["deny"]
}

path "sys/audit/*" {
  capabilities = ["deny"]
}
