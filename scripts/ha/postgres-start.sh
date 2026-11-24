#!/bin/sh
set -eu

: "${PGDATA:?missing PostgreSQL data directory}"

if [ -e "$PGDATA/.ha-fenced" ]; then
    echo "This PostgreSQL volume was fenced during failover. Rejoin using a fresh replica volume." >&2
    exit 1
fi

if [ -n "${PG_REPLICA_SOURCE:-}" ]; then
    : "${PG_REPLICATION_PASSWORD:?missing replication password}"
    # Use a passfile for both the base backup and WAL receiver. Keep the password
    # out of primary_conninfo and escape the separators understood by libpq.
    install -d -m 0700 -o postgres -g postgres /run/postgres-ha
    umask 077
    password=$(printf '%s' "$PG_REPLICATION_PASSWORD" | sed 's/[\\:]/\\&/g')
    printf '*:5432:replication:ha_replication:%s\n' "$password" > /run/postgres-ha/replication.pgpass
    chown postgres:postgres /run/postgres-ha/replication.pgpass
    unset password

    if [ ! -s "$PGDATA/PG_VERSION" ]; then
        if [ -e "$PGDATA" ]; then
            echo "Refusing to replace an existing uninitialized replica data directory" >&2
            exit 1
        fi
        staging=$(mktemp -d "${PGDATA%/*}/.ha-basebackup.XXXXXXXX")
        trap 'rm -rf "$staging"' EXIT
        chown postgres:postgres "$staging"
        gosu postgres pg_basebackup -h "$PG_REPLICA_SOURCE" -U ha_replication \
            -d 'passfile=/run/postgres-ha/replication.pgpass connect_timeout=3' \
            -D "$staging" --wal-method=stream --checkpoint=fast --no-password
        if ! grep -Fqx "include_if_exists = 'replication-upstream.conf'" "$staging/postgresql.conf"; then
            printf "\ninclude_if_exists = 'replication-upstream.conf'\n" >> "$staging/postgresql.conf"
        fi
        gosu postgres sh /ha/postgres-upstream.sh "$PG_REPLICA_SOURCE" "$staging"
        gosu postgres touch "$staging/standby.signal"
        mv "$staging" "$PGDATA"
        trap - EXIT
    fi
fi

exec docker-entrypoint.sh postgres \
    -c hba_file=/ha/postgres-hba.conf \
    -c wal_keep_size=128MB \
    -c wal_level=replica
