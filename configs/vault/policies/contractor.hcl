# =============================================================================
# contractor — time-boxed external collaborator
# =============================================================================
# The smallest grant in the lab, and the one most worth getting right: a
# contractor identity is the classic source of forgotten standing access.
#
# Read on exactly one application secret, and explicit denies everywhere a
# broader rule might later reach. Nothing writable — a contractor who needs to
# change a secret should be asking someone who owns it.
#
# Added in v2-1 so the `contractor` role profile has a Vault policy to bind to.
# The other three profiles reuse the policies that shipped with v1.
# =============================================================================

# The single application this collaborator is engaged on.
path "secret/data/apps/demo-api" {
  capabilities = ["read"]
}

path "secret/metadata/apps/demo-api" {
  capabilities = ["read"]
}

# -----------------------------------------------------------------------------
# Explicit denies
#
# `deny` wins over every other capability in Vault's policy evaluation, so these
# survive a future broadening of the grants above. That is the point: the next
# person to edit this file should have to remove a line that says "deny" rather
# than silently widen a wildcard.
# -----------------------------------------------------------------------------
path "secret/data/infrastructure/*" {
  capabilities = ["deny"]
}

path "secret/metadata/infrastructure/*" {
  capabilities = ["deny"]
}

path "secret/data/security/*" {
  capabilities = ["deny"]
}

path "secret/metadata/security/*" {
  capabilities = ["deny"]
}

path "secret/data/ci/*" {
  capabilities = ["deny"]
}

path "secret/metadata/ci/*" {
  capabilities = ["deny"]
}

# No encryption service: transit is for first-party applications.
path "transit/*" {
  capabilities = ["deny"]
}
