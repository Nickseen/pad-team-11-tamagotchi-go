#!/bin/sh
# ETL load job (#194), run by Compose as `docker compose run --rm etl` in the postgres:17-alpine image.
# 1. Applies the Warehouse migrations (warehouse/migrations, mounted at /migrations) not yet recorded.
# 2. Loads $ETL_INPUT_DIR/battle.csv and raid.csv, produced by the services' own export commands
#    (scripts/etl/extract.sh). A missing file is skipped unless ETL_REQUIRE_ALL=1. A file whose header
#    is not exactly the contract's, or with any invalid row, is rejected and loads nothing.
# Each source loads in its own transaction (etl/load-<source>.sql), so an interrupted run is repeated.
set -eu

input=${ETL_INPUT_DIR:-/input}
pause=${ETL_PAUSE_BEFORE_COMMIT:-0}
sql() { psql -X -q -v ON_ERROR_STOP=1 "$@"; }

for _ in $(seq 1 30); do pg_isready -q && break; sleep 1; done
pg_isready -q || { echo "etl: the Warehouse at $PGHOST is not reachable" >&2; exit 1; }

# Migrations: each file once, in name order, in its own transaction with its schema_migrations row.
sql -c "CREATE TABLE IF NOT EXISTS public.schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
for file in /migrations/*.sql; do
    version=$(basename "$file" .sql)
    if [ -z "$(sql -At -c "SELECT 1 FROM public.schema_migrations WHERE version = '$version'")" ]; then
        { echo "SELECT pg_advisory_xact_lock(194) AS locked \gset"; cat "$file";
          echo "INSERT INTO public.schema_migrations (version) VALUES ('$version');"; } | sql -1
        echo "etl: applied migration $version"
    fi
done

load() { # source, exact header
    file="$input/$1.csv"
    if [ ! -f "$file" ]; then
        if [ "${ETL_REQUIRE_ALL:-0}" = 1 ]; then echo "etl: $1: $file is missing" >&2; return 1; fi
        echo "etl: $1: no $file, skipped"
        return 0
    fi
    header=$(head -n 1 "$file" | tr -d '\r')
    if [ "$header" != "$2" ]; then
        echo "etl: $1: header is '$header', expected '$2'; nothing loaded" >&2
        return 1
    fi
    sql -v source_file="$(basename "$file")" -v pause="$pause" -f "/etl/load-$1.sql" < "$file"
}

status=0
load battle "battleId,outcome,finishedAt" || status=1
load raid "raidId,status,endedAt" || status=1
exit "$status"
