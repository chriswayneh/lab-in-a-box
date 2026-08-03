# =============================================================================
# developer — application engineers
# =============================================================================
# Read/write within the application's own namespace, read-only on shared
# infrastructure secrets, and no visibility into anything security-owned.
# =============================================================================

# Own your application's secrets outright.
path "secret/data/apps/*" {
  capabilities = ["create", "read", "update", "delete", "patch"]
}

path "secret/metadata/apps/*" {
  capabilities = ["read", "list", "delete"]
}

# Shared infrastructure credentials are readable, not writable — a developer
# needs the database DSN, but rotating it is the platform team's call.
path "secret/data/infrastructure/*" {
  capabilities = ["read"]
}

path "secret/metadata/infrastructure/*" {
  capabilities = ["read", "list"]
}

# Encryption as a service: use the keys, never manage them.
path "transit/encrypt/lab-app" {
  capabilities = ["create", "update"]
}

path "transit/decrypt/lab-app" {
  capabilities = ["create", "update"]
}

# Security-owned material is invisible. `deny` beats every other rule in Vault's
# policy evaluation, so this holds even if a broader grant is added later.
path "secret/data/security/*" {
  capabilities = ["deny"]
}

path "secret/metadata/security/*" {
  capabilities = ["deny"]
}
