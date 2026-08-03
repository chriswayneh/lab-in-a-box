#!/bin/bash
# =============================================================================
# PostgreSQL bootstrap — extensions
# =============================================================================
# pg_stat_statements aggregates execution statistics per normalised query,
# which is what makes "why is this slow" answerable from pgAdmin or Adminer.
# It only works if the library is preloaded at server start, which is why
# compose passes `-c shared_preload_libraries=pg_stat_statements`.
# =============================================================================
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-'SQL'
	CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
	CREATE EXTENSION IF NOT EXISTS pgcrypto;
SQL

echo "[postgres-init] extensions enabled: pg_stat_statements, pgcrypto"
