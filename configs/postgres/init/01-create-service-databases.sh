#!/bin/bash
# =============================================================================
# PostgreSQL bootstrap — one instance, one database per service
# =============================================================================
# Executed once by the official postgres entrypoint, the first time the data
# directory is initialised. Re-running `docker compose up` does NOT re-run it;
# to force a re-run you must destroy the postgres volume (`make clean`).
#
# Each service gets its own role and its own database, so a compromised or
# buggy service cannot read another service's tables. Roles are created with
# NOCREATEDB/NOCREATEROLE and are granted rights only on their own database.
# =============================================================================
set -euo pipefail

# Every service that needs a database, as "dbname:rolename:password_env_var".
SERVICES=(
  "keycloak:keycloak:KEYCLOAK_DB_PASSWORD"
  "gitea:gitea:GITEA_DB_PASSWORD"
)

log() { echo "[postgres-init] $*"; }

provision_service() {
  local db="$1" role="$2" password_var="$3"
  local password="${!password_var:-}"

  if [[ -z "$password" ]]; then
    log "WARNING: $password_var is unset; falling back to a derived value."
    log "         Run 'make secrets' for cryptographically random credentials."
    password="${role}-insecure-dev-only"
  fi

  log "provisioning database '$db' owned by role '$role'"

  # Role creation, without a DO block.
  #
  # The obvious implementation — a PL/pgSQL DO block guarding CREATE ROLE — is
  # a trap here: psql substitutes :'variables' only OUTSIDE quoted strings, and
  # a DO block body is one big dollar-quoted literal. The variables would
  # arrive at the server as the literal text :'role', producing
  # "syntax error at or near :" and aborting cluster initialisation.
  #
  # Instead, SELECT builds the statement as text (with format's %I/%L doing the
  # identifier and literal quoting correctly, including passwords containing
  # quotes) and \gexec runs the result. The WHERE clause makes it idempotent:
  # if the role exists, zero rows come back and nothing is executed.
  psql -v ON_ERROR_STOP=1 \
       --username "$POSTGRES_USER" \
       --dbname "$POSTGRES_DB" \
       --set role="$role" \
       --set password="$password" <<-'SQL'
		SELECT format(
		         'CREATE ROLE %I LOGIN PASSWORD %L NOCREATEDB NOCREATEROLE NOSUPERUSER',
		         :'role', :'password')
		WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role');
		\gexec
	SQL

  # CREATE DATABASE cannot run inside a transaction block, hence the separate call.
  if ! psql -tAc "SELECT 1 FROM pg_database WHERE datname = '$db'" \
            --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" | grep -q 1; then
    createdb --username "$POSTGRES_USER" --owner "$role" "$db"
  fi

  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" \
       --set role="$role" <<-'SQL'
		-- Revoke the permissive default that lets any role create objects in public.
		REVOKE ALL ON SCHEMA public FROM PUBLIC;
		GRANT ALL ON SCHEMA public TO :"role";
	SQL
}

for entry in "${SERVICES[@]}"; do
  IFS=':' read -r db role password_var <<< "$entry"
  provision_service "$db" "$role" "$password_var"
done

# A read-only role for humans poking around in pgAdmin / Adminer, so that
# exploratory queries cannot mutate service data.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-'SQL'
	DO $$
	BEGIN
	  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'lab_readonly') THEN
	    CREATE ROLE lab_readonly LOGIN PASSWORD 'readonly' NOSUPERUSER NOCREATEDB NOCREATEROLE;
	  END IF;
	END
	$$;
	GRANT CONNECT ON DATABASE lab TO lab_readonly;
	GRANT USAGE ON SCHEMA public TO lab_readonly;
	ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO lab_readonly;
SQL

log "bootstrap complete: ${#SERVICES[@]} service databases ready"
