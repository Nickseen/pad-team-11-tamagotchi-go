#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/pad-ha-test.XXXXXXXX")
# A generated Compose project keeps these deliberate failures and volume
# cleanup away from the regular stack, even when it is running.
export COMPOSE_PROJECT_NAME="pad-ha-test-$(printf '%s' "${work##*.}" | tr '[:upper:]' '[:lower:]')"
export HA_ENV_FILE="$work/env"
export RAID_DB_PASSWORD=disposable-raid-password MAP_REDIS_PASSWORD=disposable-map-password
export RAID_REPLICATION_PASSWORD=disposable-replication-password
unset RAID_POSTGRES_VOLUME RAID_POSTGRES_DATA_DIR RAID_POSTGRES_UPSTREAM
cp "$root/.env.example" "$HA_ENV_FILE"

compose() {
    docker compose --project-name "$COMPOSE_PROJECT_NAME" --env-file "$HA_ENV_FILE" \
        -f "$root/docker-compose.yml" "$@"
}

rejoined=0
seed_postgres="$COMPOSE_PROJECT_NAME-seed-postgres"
seed_redis="$COMPOSE_PROJECT_NAME-seed-redis"
on_rejoined_volume() (
    export RAID_POSTGRES_VOLUME=raid-rejoined-data
    export RAID_POSTGRES_DATA_DIR=/var/lib/postgresql/data/pgdata
    export RAID_POSTGRES_UPSTREAM=monster-raid-db-1
    "$@"
)

cleanup() {
    docker stop "$seed_postgres" "$seed_redis" >/dev/null 2>&1 || :
    if (( rejoined )); then
        on_rejoined_volume compose down --volumes --remove-orphans
    fi
    compose down --volumes --remove-orphans
    rm -f "$HA_ENV_FILE"
    rmdir "$work"
}
trap cleanup EXIT

wait_value() {
    local expected=$1 message=$2 value=
    shift 2
    for (( attempt = 0; attempt < 40; attempt++ )); do
        value=$("$@" 2>/dev/null) || value=
        if [[ "$value" == "$expected" ]]; then
            printf '%s\n' "$message"
            return 0
        fi
        sleep 1
    done
    printf '%s (last result: %s)\n' "$message" "$value" >&2
    return 1
}

pg() {
    local node=$1 statement=$2
    compose exec -T "$node" psql -XAt -v ON_ERROR_STOP=1 -U raid -d raid -c "$statement"
}

pg_writer() {
    compose exec -T monster-raid-db-2 sh -ec '
        export PGPASSWORD="$POSTGRES_PASSWORD"
        psql -XAt -v ON_ERROR_STOP=1 -U raid -c "$1" -d \
          "dbname=raid host=monster-raid-db,monster-raid-db-1,monster-raid-db-2 target_session_attrs=read-write connect_timeout=2"
    ' sh "$1"
}

redis_writer() {
    compose exec -T map-sentinel-1 redis-cli -e --raw -h map-redis-writer "$@"
}

redis_role() {
    compose exec -T "$1" redis-cli -e --raw ROLE | sed -n '1p'
}

# Seed the same named volumes using standalone upstream server entrypoints.
# Start the normal project services only after the seed containers stop.
compose run -d --rm --no-deps --name "$seed_postgres" --entrypoint docker-entrypoint.sh \
    monster-raid-db postgres
compose run -d --rm --no-deps --name "$seed_redis" --entrypoint sh map-redis -ec \
    'exec docker-entrypoint.sh redis-server --appendonly yes --requirepass "$MAP_REDIS_PASSWORD"'
wait_value ready 'Standalone PostgreSQL is ready for the upgrade fixture' \
    docker exec "$seed_postgres" sh -ec 'pg_isready -q -h 127.0.0.1 -U raid -d raid; echo ready'
docker exec "$seed_postgres" psql -XAt -v ON_ERROR_STOP=1 -U raid -d raid \
    -c 'CREATE TABLE ha_check (id integer PRIMARY KEY); INSERT INTO ha_check VALUES (1);'
wait_value PONG 'Standalone Redis is ready for the upgrade fixture' \
    docker exec "$seed_redis" redis-cli -e PING
docker exec "$seed_redis" redis-cli -e SET ha:map:check before EX 180
docker stop "$seed_postgres" "$seed_redis"
compose up -d --wait --wait-timeout 90 monster-raid-db-1 monster-raid-db-2 map-redis-writer

for standby in monster-raid-db-1 monster-raid-db-2; do
    wait_value 't|1' "PostgreSQL $standby contains the pre-upgrade row" \
        pg "$standby" 'SELECT pg_is_in_recovery(), id FROM ha_check'
done
wait_value before 'Redis writer contains the pre-upgrade key' redis_writer GET ha:map:check
for replica in map-redis-1 map-redis-2; do
    wait_value before "Redis $replica contains the pre-upgrade key" \
        compose exec -T "$replica" redis-cli -e --raw GET ha:map:check
done

bash "$root/scripts/ha/postgres-promote.sh" monster-raid-db monster-raid-db-1 monster-raid-db-2
wait_value 'f|1' 'The same PostgreSQL connection reaches the promoted writer and reads the pre-failure row' \
    pg_writer 'SELECT pg_is_in_recovery(), id FROM ha_check'
pg_writer 'INSERT INTO ha_check VALUES (2)'
wait_value 't|2' 'The other PostgreSQL standby follows the promoted writer' \
    pg monster-raid-db-2 'SELECT pg_is_in_recovery(), max(id) FROM ha_check'

rejoined=1
on_rejoined_volume compose up -d --no-deps --wait --wait-timeout 90 monster-raid-db
wait_value 't|2' 'The old PostgreSQL name rejoins as a standby on a new volume' \
    pg monster-raid-db 'SELECT pg_is_in_recovery(), max(id) FROM ha_check'

compose stop map-redis
wait_value before 'The same Redis connection reads the pre-failure key after Sentinel promotion' \
    redis_writer GET ha:map:check
ttl=$(redis_writer TTL ha:map:check)
(( ttl > 0 && ttl < 180 ))
wait_value OK 'The Redis writer accepts new data after promotion' redis_writer SET ha:map:new after
redis_writer SET ha:map:expires soon EX 1
wait_value 0 'Redis TTL still expires a key after promotion' redis_writer EXISTS ha:map:expires
compose start map-redis
wait_value slave 'The old Redis writer rejoins as a replica' redis_role map-redis
