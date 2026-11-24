#!/bin/sh
set -eu

node=$1
data=$2
case "$node" in
    ''|*[!a-zA-Z0-9.-]*) echo "Invalid PostgreSQL upstream hostname" >&2; exit 1 ;;
esac

umask 077
file=$(mktemp "$data/.replication-upstream.XXXXXXXX")
trap 'rm -f "$file"' EXIT
printf "primary_conninfo = 'host=%s port=5432 user=ha_replication passfile=/run/postgres-ha/replication.pgpass application_name=%s connect_timeout=3'\n" \
    "$node" "$(hostname)" > "$file"
mv "$file" "$data/replication-upstream.conf"
