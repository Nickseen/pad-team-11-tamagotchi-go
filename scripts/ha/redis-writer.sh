#!/bin/sh
set -eu

: "${MAP_REDIS_PASSWORD:?missing Map Redis password}"
umask 077
config=$(mktemp)
bytes=$(printf '%s' "$MAP_REDIS_PASSWORD" | wc -c | tr -d ' ')
auth=$(printf '*2\r\n$4\r\nAUTH\r\n$%s\r\n%s\r\n' "$bytes" "$MAP_REDIS_PASSWORD" |
    od -An -v -tx1 | tr -d ' \n')

# The binary frames keep the Redis password opaque to the HAProxy config parser.
# Check the writer role and a connected replica. A returning old primary
# remains unroutable while Sentinel is reconfiguring it as a replica.
cat > "$config" <<EOF
defaults
    mode tcp
    timeout connect 1s
    timeout client 5m
    timeout server 5m
    timeout check 1s

resolvers docker
    parse-resolv-conf
    hold valid 1s

frontend redis
    bind :6379
    default_backend writer

backend writer
    option tcp-check
    tcp-check connect
    tcp-check send-binary $auth
    tcp-check expect binary 2b4f4b0d0a
    tcp-check send-binary 2a310d0a24340d0a524f4c450d0a
    tcp-check expect binary 24360d0a6d61737465720d0a
    tcp-check send-binary 2a320d0a24340d0a494e464f0d0a2431310d0a7265706c69636174696f6e0d0a
    tcp-check expect rstring connected_slaves:[1-9]
    default-server check inter 1s fall 2 rise 1 resolvers docker init-addr none init-state fully-down on-marked-down shutdown-sessions
    server map-redis map-redis:6379
    server map-redis-1 map-redis-1:6379
    server map-redis-2 map-redis-2:6379
EOF

exec haproxy -db -f "$config"
