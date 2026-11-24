#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
old=${1:?provide the failed primary Compose service}
candidate=${2:?provide the standby to promote}
remaining=${3:?provide the other standby}

if [[ "$old" == "$candidate" || "$old" == "$remaining" || "$candidate" == "$remaining" ]]; then
    echo "Provide three different Compose services" >&2
    exit 1
fi

compose() {
    docker compose --env-file "${LAB5_HA_ENV_FILE:-$root/.env}" \
        -f "$root/docker-compose.yml" -f "$root/docker-compose.lab5-ha.yml" "$@"
}

query() {
    local service=$1 statement=$2
    compose exec -T "$service" sh -ec \
        'psql -XAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"' sh "$statement"
}

candidate_role=$(query "$candidate" 'SELECT pg_is_in_recovery()')
old_role=$(query "$old" 'SELECT pg_is_in_recovery()' 2>/dev/null || :)
if [[ "$candidate_role" != t && "$candidate_role" != f ]] ||
   [[ "$old_role" == t || $(query "$remaining" 'SELECT pg_is_in_recovery()') != t ]]; then
    echo "Expected the old writer, a reachable candidate and a separate standby" >&2
    exit 1
fi

# Fence the old data directory after stopping its container. A later Compose up
# will refuse to start that volume, including after a retry of this command.
compose stop "$old"
compose run -T --rm --no-deps --entrypoint sh "$old" -ec \
    'test -s "$PGDATA/PG_VERSION"; touch "$PGDATA/.lab5-fenced"'

if [[ "$candidate_role" == t ]]; then
    candidate_lsn=$(query "$candidate" 'SELECT pg_last_wal_replay_lsn()')
    remaining_lsn=$(query "$remaining" 'SELECT pg_last_wal_replay_lsn()')
    lsn_pattern='^[[:xdigit:]]+/[[:xdigit:]]+$'
    if ! [[ "$candidate_lsn" =~ $lsn_pattern && "$remaining_lsn" =~ $lsn_pattern ]] ||
       [[ $(query "$candidate" "SELECT '$candidate_lsn'::pg_lsn >= '$remaining_lsn'::pg_lsn") != t ]]; then
        echo "Candidate is behind the other standby or lacks a replay position. Choose the most current standby." >&2
        exit 1
    fi
fi

# Atomically record the chosen candidate on the fenced volume so two operators
# cannot promote different standbys. Retrying the same choice is supported.
compose run -T --rm --no-deps --entrypoint sh "$old" -ec '
    test -f "$PGDATA/.lab5-fenced"
    choice=$(mktemp "$PGDATA/.lab5-choice.XXXXXXXX")
    trap "rm -f \"$choice\"" EXIT
    printf "%s\n" "$1" > "$choice"
    if ! ln "$choice" "$PGDATA/.lab5-promotion" 2>/dev/null; then
        if [ "$(cat "$PGDATA/.lab5-promotion")" != "$1" ]; then
            echo "A different standby has already been selected for this failover" >&2
            exit 1
        fi
    fi
' sh "$candidate"

if [[ $(query "$candidate" 'SELECT pg_is_in_recovery()') == t ]]; then
    compose exec -T -u postgres "$candidate" sh -ec 'pg_ctl -D "$PGDATA" -w promote'
fi

compose exec -T -u postgres "$remaining" sh -ec \
    'sh /lab5-ha/postgres-upstream.sh "$1" "$PGDATA"; pg_ctl -D "$PGDATA" reload' sh "$candidate"
echo "PostgreSQL writer: $candidate. Following it: $remaining. Fenced: $old."
