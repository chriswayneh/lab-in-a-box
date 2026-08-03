# =============================================================================
# security-analyst — detection and response
# =============================================================================
# Reads widely so incidents can actually be investigated, but writes almost
# nothing. An analyst who can silently modify a secret is an analyst whose
# findings cannot be trusted.
# =============================================================================

# Read-only across the secret tree, including the security namespace.
path "secret/data/*" {
  capabilities = ["read"]
}

path "secret/metadata/*" {
  capabilities = ["read", "list"]
}

# Security's own working area is read/write.
path "secret/data/security/*" {
  capabilities = ["create", "read", "update", "patch"]
}

path "secret/metadata/security/*" {
  capabilities = ["read", "list"]
}

# Visibility into how the vault itself is configured — mounts, auth methods and
# policies are all part of the attack surface being reviewed.
path "sys/auth" {
  capabilities = ["read", "list"]
}

path "sys/mounts" {
  capabilities = ["read", "list"]
}

path "sys/policies/acl" {
  capabilities = ["read", "list"]
}

path "sys/policies/acl/*" {
  capabilities = ["read"]
}

# Read the audit configuration to verify logging is enabled and shipping —
# without the ability to turn it off.
path "sys/audit" {
  capabilities = ["read", "list", "sudo"]
}
