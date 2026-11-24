#!/bin/sh
set -eu

node=$1
data=$2
case "$node" in
    ''|*[!a-zA-Z0-9.-]*) echo "Invalid PostgreSQL upstream hostname" >&2; exit 1 ;;
esac

umask 077
file=$(mktemp "$data/.lab5-upstream.XXXXXXXX")
trap 'rm -f "$file"' EXIT
printf "primary_conninfo = 'host=%s port=5432 user=lab5_replication passfile=/run/lab5-postgres/replication.pgpass application_name=%s connect_timeout=3'\n" \
    "$node" "$(hostname)" > "$file"
mv "$file" "$data/lab5-upstream.conf"
