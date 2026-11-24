#!/bin/sh
set -eu

: "${PGHOST:?missing initial primary hostname}"
: "${POSTGRES_DB:?missing database}"
: "${POSTGRES_USER:?missing database owner}"
: "${POSTGRES_PASSWORD:?missing database password}"
: "${PG_REPLICATION_PASSWORD:?missing replication password}"
export PGPASSWORD="$POSTGRES_PASSWORD"

psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<'SQL'
\getenv replication_password PG_REPLICATION_PASSWORD
SELECT format('CREATE ROLE lab5_replication LOGIN REPLICATION PASSWORD %L', :'replication_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'lab5_replication')
\gexec
SQL
