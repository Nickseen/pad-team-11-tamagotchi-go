#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/pad-lab5-ha.XXXXXXXX")
# A generated Compose project keeps these deliberate failures and volume
# cleanup away from the regular stack, even when it is running.
export COMPOSE_PROJECT_NAME="pad-lab5-ha-$(printf '%s' "${work##*.}" | tr '[:upper:]' '[:lower:]')"
export LAB5_HA_ENV_FILE="$work/env"
export RAID_DB_PASSWORD=disposable-raid-password MAP_REDIS_PASSWORD=disposable-map-password
export LAB5_RAID_REPLICATION_PASSWORD=disposable-replication-password
unset LAB5_RAID_ORIGINAL_VOLUME LAB5_RAID_ORIGINAL_DATA_DIR LAB5_RAID_REJOIN_SOURCE
cp "$root/.env.example" "$LAB5_HA_ENV_FILE"

base() {
    docker compose --project-name "$COMPOSE_PROJECT_NAME" --env-file "$LAB5_HA_ENV_FILE" \
        -f "$root/docker-compose.yml" "$@"
}

ha() {
    base -f "$root/docker-compose.lab5-ha.yml" "$@"
}

rejoined=0
on_rejoined_volume() (
    export LAB5_RAID_ORIGINAL_VOLUME=raid-rejoined-data
    export LAB5_RAID_ORIGINAL_DATA_DIR=/var/lib/postgresql/data/pgdata
    export LAB5_RAID_REJOIN_SOURCE=monster-raid-db-1
    "$@"
)

cleanup() {
    if (( rejoined )); then
        on_rejoined_volume ha down --volumes --remove-orphans
    fi
    ha down --volumes --remove-orphans
    rm -f "$LAB5_HA_ENV_FILE"
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
    ha exec -T "$node" psql -XAt -v ON_ERROR_STOP=1 -U raid -d raid -c "$statement"
}

pg_writer() {
    ha exec -T monster-raid-db-2 sh -ec '
        export PGPASSWORD="$POSTGRES_PASSWORD"
        psql -XAt -v ON_ERROR_STOP=1 -U raid -c "$1" -d \
          "dbname=raid host=monster-raid-db,monster-raid-db-1,monster-raid-db-2 target_session_attrs=read-write connect_timeout=2"
    ' sh "$1"
}

redis_writer() {
    ha exec -T map-sentinel-1 redis-cli -e --raw -h map-redis-writer "$@"
}

redis_role() {
    ha exec -T "$1" redis-cli -e --raw ROLE | sed -n '1p'
}

base up -d --wait --wait-timeout 90 monster-raid-db map-redis
pg monster-raid-db 'CREATE TABLE ha_check (id integer PRIMARY KEY); INSERT INTO ha_check VALUES (1);'
ha exec -T map-redis redis-cli -e SET lab5:map:check before EX 180
ha up -d --wait --wait-timeout 90 monster-raid-db-1 monster-raid-db-2 map-redis-writer

for standby in monster-raid-db-1 monster-raid-db-2; do
    wait_value 't|1' "PostgreSQL $standby contains the pre-upgrade row" \
        pg "$standby" 'SELECT pg_is_in_recovery(), id FROM ha_check'
done
wait_value before 'Redis writer contains the pre-upgrade key' redis_writer GET lab5:map:check
for replica in map-redis-1 map-redis-2; do
    wait_value before "Redis $replica contains the pre-upgrade key" \
        ha exec -T "$replica" redis-cli -e --raw GET lab5:map:check
done

bash "$root/scripts/lab5/ha/postgres-promote.sh" monster-raid-db monster-raid-db-1 monster-raid-db-2
wait_value 'f|1' 'The same PostgreSQL connection reaches the promoted writer and reads the pre-failure row' \
    pg_writer 'SELECT pg_is_in_recovery(), id FROM ha_check'
pg_writer 'INSERT INTO ha_check VALUES (2)'
wait_value 't|2' 'The other PostgreSQL standby follows the promoted writer' \
    pg monster-raid-db-2 'SELECT pg_is_in_recovery(), max(id) FROM ha_check'

rejoined=1
on_rejoined_volume ha up -d --no-deps --wait --wait-timeout 90 monster-raid-db
wait_value 't|2' 'The old PostgreSQL name rejoins as a standby on a new volume' \
    pg monster-raid-db 'SELECT pg_is_in_recovery(), max(id) FROM ha_check'

ha stop map-redis
wait_value before 'The same Redis connection reads the pre-failure key after Sentinel promotion' \
    redis_writer GET lab5:map:check
ttl=$(redis_writer TTL lab5:map:check)
(( ttl > 0 && ttl < 180 ))
wait_value OK 'The Redis writer accepts new data after promotion' redis_writer SET lab5:map:new after
redis_writer SET lab5:map:expires soon EX 1
wait_value 0 'Redis TTL still expires a key after promotion' redis_writer EXISTS lab5:map:expires
ha start map-redis
wait_value slave 'The old Redis writer rejoins as a replica' redis_role map-redis
