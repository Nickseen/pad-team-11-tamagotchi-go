#!/usr/bin/env bash
# Service-level PostgreSQL failover check for Tamagotchi, Notification, Package Registry and Guild (#193):
#   bash scripts/ha/check-service-failover.sh [tamagotchi|notification|package-registry|guild ...]
# For each service it starts the primary and both standbys in a disposable Compose project, runs the
# service image pinned in docker-compose.yml with the Compose connection string, writes through the
# service, kills the primary, promotes a standby with postgres-promote.sh, and then reads the earlier
# write and makes a new one through the same running service. Everything it creates is removed at exit.
# TAMAGOTCHI_IMAGE / NOTIFICATION_IMAGE / PACKAGE_REGISTRY_IMAGE / GUILD_IMAGE test another image than
# the pinned one, e.g. a local build.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/pad-svc-ha.XXXXXXXX")
export COMPOSE_PROJECT_NAME="pad-svc-ha-$(printf '%s' "${work##*.}" | tr '[:upper:]' '[:lower:]')"
export HA_ENV_FILE="$work/env"
secret=disposable-failover-secret
for name in TAMAGOTCHI_DB_PASSWORD NOTIFICATION_DB_PASSWORD TAMAGOTCHI_REPLICATION_PASSWORD \
    NOTIFICATION_REPLICATION_PASSWORD REGISTRY_DB_PASSWORD REGISTRY_REPLICATION_PASSWORD \
    GUILD_DB_PASSWORD GUILD_REPLICATION_PASSWORD; do
    export "$name=$secret"
done
unset TAMAGOTCHI_POSTGRES_VOLUME TAMAGOTCHI_POSTGRES_DATA_DIR TAMAGOTCHI_POSTGRES_UPSTREAM
unset NOTIFICATION_POSTGRES_VOLUME NOTIFICATION_POSTGRES_DATA_DIR NOTIFICATION_POSTGRES_UPSTREAM
unset REGISTRY_POSTGRES_VOLUME REGISTRY_POSTGRES_DATA_DIR REGISTRY_POSTGRES_UPSTREAM
unset GUILD_POSTGRES_VOLUME GUILD_POSTGRES_DATA_DIR GUILD_POSTGRES_UPSTREAM
# The rest of .env.example only has to parse; this check never starts those services.
sed 's/=CHANGE_ME$/=disposable-unused-secret/' "$root/.env.example" > "$HA_ENV_FILE"

compose() {
    docker compose --project-name "$COMPOSE_PROJECT_NAME" --env-file "$HA_ENV_FILE" \
        -f "$root/docker-compose.yml" "$@"
}

cleanup() {
    docker rm -f "$COMPOSE_PROJECT_NAME-tamagotchi" "$COMPOSE_PROJECT_NAME-notification" \
        "$COMPOSE_PROJECT_NAME-package-registry" "$COMPOSE_PROJECT_NAME-guild" >/dev/null 2>&1 || :
    compose down -v --remove-orphans >/dev/null 2>&1 || :
    rm -rf "$work"
}
trap cleanup EXIT

query() {
    compose exec -T "$1" sh -ec 'psql -XAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"' sh "$2"
}

# The service's image and DATABASE_URL exactly as docker-compose.yml defines them.
service_setting() {
    compose config --format json | python3 -c \
        'import json, sys; s = json.load(sys.stdin)["services"][sys.argv[1]]; print(s["image"] if sys.argv[2] == "image" else s["environment"][sys.argv[2]])' \
        "$1" "$2"
}

user=11111111-1111-4111-8111-111111111111

check() {
    local svc=$1 port table image
    # Guild keeps one Compose block per replica (its WebSocket host ports); its settings are guild-1's.
    local config=$svc
    [ "$svc" = guild ] && config=guild-1
    case "$svc" in
        tamagotchi) port=8082 table=tamagotchis image=${TAMAGOTCHI_IMAGE:-} ;;
        notification) port=8086 table=devices image=${NOTIFICATION_IMAGE:-} ;;
        package-registry) port=8083 table=packages image=${PACKAGE_REGISTRY_IMAGE:-} ;;
        guild) port=8087 table=guilds image=${GUILD_IMAGE:-} ;;
        *) echo "unknown service $svc (tamagotchi, notification, package-registry or guild)" >&2; return 1 ;;
    esac
    echo "== $svc"
    compose up -d --wait "$svc-db" "$svc-db-1" "$svc-db-2" >/dev/null 2>&1
    echo "roles: primary $(query "$svc-db" 'SELECT NOT pg_is_in_recovery()'), standbys $(query "$svc-db-1" 'SELECT pg_is_in_recovery()') $(query "$svc-db-2" 'SELECT pg_is_in_recovery()')"

    local app="$COMPOSE_PROJECT_NAME-$svc"
    image=${image:-$(service_setting "$config" image)}
    docker image inspect "$image" >/dev/null 2>&1 || docker pull -q "$image" >/dev/null
    docker run -d --name "$app" --network "${COMPOSE_PROJECT_NAME}_default" \
        -e DATABASE_URL="$(service_setting "$config" DATABASE_URL)" -e GATEWAY_TOKEN="$secret" \
        -e SERVICE_TOKEN="$secret" -e TASK_TIMEOUT_MS=3000 -e MAX_CONCURRENT_TASKS=10 \
        "$image" >/dev/null
    echo "image: $image"
    call() { # METHOD PATH [BODY]: "<status> <body>" from inside the service's container
        docker exec "$app" python -c 'import json, sys, urllib.request
method, path, body = sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None
request = urllib.request.Request("http://127.0.0.1:" + sys.argv[4] + path, method=method,
    data=body.encode() if body else None, headers={"X-Gateway-Token": sys.argv[5],
    "X-Authenticated-User-Id": sys.argv[6], "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(request, timeout=5) as answer: print(answer.status, answer.read().decode())
except urllib.error.HTTPError as error: print(error.code, error.read().decode())' "$1" "$2" "${3:-}" "$port" "$secret" "$user"
    }
    for _ in $(seq 1 40); do docker exec "$app" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$port/health')" 2>/dev/null && break; sleep 1; done

    local collection body
    case "$svc" in
        tamagotchi)
            collection=/api/v1/tamagotchis
            body() { printf '{"packageId":"22222222-2222-4222-8222-222222222222","name":"%s","combatType":"flame","spriteRef":"s.png","stats":{"hunger":5}}' "$1"; } ;;
        notification)
            collection=/api/v1/devices
            body() { printf '{"fcmToken":"%s","platform":"android","locale":"ro-MD"}' "$1"; } ;;
        package-registry)
            collection=/api/v1/packages
            body() { printf '{"slug":"%s","name":"%s","description":"failover check"}' "$1" "$1"; } ;;
        guild) # tag: 2-5 characters, unique (BEX before, AFX after)
            collection=/api/v1/guilds
            body() { printf '{"name":"%s","tag":"%sX","description":"failover check"}' "$1" "$(printf '%s' "$1" | cut -c1-2 | tr '[:lower:]' '[:upper:]')"; } ;;
    esac
    count() { call GET "$collection" | python3 -c 'import json, sys; status, _, body = sys.stdin.read().partition(" "); print(len(json.loads(body)["items"]) if status == "200" else "status " + status)'; }

    echo "write before primary loss: $(call POST "$collection" "$(body before-failover)" | cut -d' ' -f1)"
    for _ in $(seq 1 20); do
        rows=$(query "$svc-db" "SELECT count(*) FROM $table")
        [ "$(query "$svc-db-1" "SELECT count(*) FROM $table")" = "$rows" ] &&
            [ "$(query "$svc-db-2" "SELECT count(*) FROM $table")" = "$rows" ] && break
        sleep 1
    done
    echo "both standbys replayed it: $(query "$svc-db-1" "SELECT count(*) FROM $table") and $(query "$svc-db-2" "SELECT count(*) FROM $table") rows"

    compose kill "$svc-db" >/dev/null 2>&1
    bash "$root/scripts/ha/postgres-promote.sh" "$svc-db" "$svc-db-1" "$svc-db-2" 2>&1 | tail -1
    local promoted
    promoted=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    echo "read after failover, same running service: $(count) record(s)"
    echo "write after failover: $(call POST "$collection" "$(body after-failover)" | cut -d' ' -f1)"
    echo "read again: $(count) record(s)"
    # Readiness opens a fresh connection on each check, so it also shows whether new connections reach the
    # new writer while the fenced primary's name no longer resolves; SD and the Broker route by it.
    local probes=""
    for _ in 1 2 3; do probes="$probes $(call GET /api/v1/status | cut -d' ' -f1)"; sleep 2; done
    echo "readiness after failover (GET /api/v1/status x3):$probes"
    sleep 1
    echo "new writer $svc-db-1: $(query "$svc-db-1" "SELECT count(*) FROM $table") rows; following standby $svc-db-2: $(query "$svc-db-2" "SELECT count(*) FROM $table") rows"
    # Package Registry and Guild publish events through the Broker, which this check does not start:
    # their event.publish_failed lines are expected here and are not database errors.
    echo "service error lines: $(docker logs "$app" 2>&1 | grep -E '"level": ?"(ERROR|error)"' |
        grep -c -v -E '"event": ?"event.publish_failed"' || :)"
    # Background work (e.g. an outbox poll) may log errors while no primary exists; after it, none should.
    echo "service error lines after the promotion: $(docker logs --since "$promoted" "$app" 2>&1 |
        grep -E '"level": ?"(ERROR|error)"' | grep -c -v -E '"event": ?"event.publish_failed"' || :)"
}

for svc in "${@:-tamagotchi notification package-registry guild}"; do
    for one in $svc; do check "$one"; done
done
