#!/bin/sh
set -eu

: "${MAP_REDIS_PASSWORD:?missing Map Redis password}"
case "$MAP_REDIS_PASSWORD" in
    *'
'*) echo "Map Redis password must be a single line" >&2; exit 1 ;;
esac

umask 077
password=$(printf '%s' "$MAP_REDIS_PASSWORD" | sed 's/[\\"]/\\&/g')
config=/data/redis-ha.conf

if [ ! -s "$config" ]; then
    pending_config=$(mktemp /data/.ha-redis.XXXXXXXX)
    trap 'rm -f "$pending_config"' EXIT
    if [ "$1" = sentinel ]; then
        cat > "$pending_config" <<EOF
port 26379
dir /data
requirepass "$password"
sentinel resolve-hostnames yes
sentinel announce-hostnames yes
sentinel announce-ip $(hostname)
sentinel monitor map map-redis 6379 2
sentinel auth-pass map "$password"
sentinel down-after-milliseconds map 3000
sentinel failover-timeout map 30000
EOF
    else
        cat > "$pending_config" <<EOF
dir /data
appendonly yes
requirepass "$password"
masterauth "$password"
replica-announce-ip $(hostname)
min-replicas-to-write 1
min-replicas-max-lag 5
EOF
        if [ "$1" = replica ]; then
            printf 'replicaof map-redis 6379\n' >> "$pending_config"
        fi
    fi
    mv "$pending_config" "$config"
    trap - EXIT
fi

if [ "$1" = sentinel ]; then
    exec docker-entrypoint.sh redis-server "$config" --sentinel
fi
exec docker-entrypoint.sh redis-server "$config"
