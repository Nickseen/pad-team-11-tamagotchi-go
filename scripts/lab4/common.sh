#!/usr/bin/env bash
# Shared helpers for the Lab 4 demo scripts. Run them from anywhere with bash (Git Bash on Windows):
#   bash scripts/lab4/least-load.sh
# They need the stack running (docker compose up -d) and the Broker published on 127.0.0.1:8091.
# Extra Compose files (e.g. a local override) can be passed as COMPOSE_FILES="-f a.yml -f b.yml".
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
export MSYS_NO_PATHCONV=1 # Git Bash: keep container paths as they are

SERVICE_TOKEN="$(grep -E '^SERVICE_TOKEN=' .env | cut -d= -f2-)"
GATEWAY_TOKEN="$(grep -E '^GATEWAY_TOKEN=' .env | cut -d= -f2-)"
BROKER="http://127.0.0.1:8091"
ZERO="00000000-0000-4000-8000-000000000000"

dc() { docker compose -f docker-compose.yml ${COMPOSE_FILES:-} "$@"; }
uuid() { # a random version-4 UUID with bash only
  local h; h="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
  printf '%s-%s-4%s-%x%s-%s\n' "${h:0:8}" "${h:8:4}" "${h:13:3}" $(( (16#${h:16:1} & 3) | 8 )) "${h:17:3}" "${h:20:12}"
}
now() { date -u +%Y-%m-%dT%H:%M:%S.000Z; }
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

# GET through a service's Broker request queue as a service caller; prints "<status> <instance8>".
queue_get() {
  # -i, not -o /dev/null: with MSYS_NO_PATHCONV set, Windows curl cannot open /dev/null.
  curl -s -i -H "X-Service-Token: $SERVICE_TOKEN" -H "X-Correlation-Id: $(uuid)" \
    "$BROKER/api/v1/queues/$1$2" | tr -d '\r' |
    awk 'tolower($1)=="x-upstream-instance-id:" {i=substr($2,1,8)} /^HTTP/ {s=$2} END {print s, (i ? i : "-")}'
}

# Client-style call through the Broker (as Gateway 4.x sends it): Gateway token plus a trusted user.
client_call() { # method service path user [json]
  curl -s -X "$1" -H "X-Service-Token: $SERVICE_TOKEN" -H "X-Gateway-Token: $GATEWAY_TOKEN" \
    -H "X-Authenticated-User-Id: $4" -H "X-Authenticated-Roles: user" -H "Content-Type: application/json" \
    -H "X-Correlation-Id: $(uuid)" ${5:+-d "$5"} "$BROKER/api/v1/queues/$2$3"
}

publish() { # topic json
  curl -s -X POST -H "X-Service-Token: $SERVICE_TOKEN" -H "Content-Type: application/json" \
    -H "X-Correlation-Id: $(uuid)" -d "$2" "$BROKER/api/v1/topics/$1/messages"
}

broker_get() { curl -s -H "X-Service-Token: $SERVICE_TOKEN" "$BROKER$1"; }
queue_depth() { curl -s "$BROKER/metrics" | awk -v q="queue=\"$1\"" 'index($0, "broker_topic_queue_depth{") == 1 && index($0, q) {print $NF; f=1} END {if (!f) print 0}'; }
metric() { curl -s "$BROKER/metrics" | awk -v m="$1" '$1 ~ "^"m {s+=$NF} END {print s+0}'; }
json_get() { sed -n "s/.*\"$1\": *\"\{0,1\}\([^\",}]*\).*/\1/p" | head -1; } # first value of a key

# A guild with one member, created through the Broker; prints "<guildId> <memberId> <ownerId>".
guild_with_member() {
  local owner member tag guild invitation
  owner="$(uuid)"; member="$(uuid)"; tag="$(uuid | tr -dc 'a-f0-9' | head -c 5 | tr a-f A-F)"
  guild="$(client_call POST guild /api/v1/guilds "$owner" "{\"name\":\"Demo $tag\",\"tag\":\"$tag\"}" | json_get guildId)"
  invitation="$(client_call POST guild "/api/v1/guilds/$guild/invitations" "$owner" "{\"userId\":\"$member\"}" | json_get invitationId)"
  client_call POST guild "/api/v1/invitations/$invitation/accept" "$member" >/dev/null
  echo "$guild $member $owner"
}

member_count() { client_call GET guild "/api/v1/guilds/$1" "$2" | json_get memberCount; }

user_deleted() { # userId [eventVersion]
  local t; t="$(now)"
  echo "{\"eventId\":\"$(uuid)\",\"eventType\":\"user.deleted\",\"eventVersion\":${2:-1},\"occurredAt\":\"$t\",\
\"producer\":\"user-management\",\"correlationId\":\"$(uuid)\",\"idempotencyKey\":\"$1\",\
\"payload\":{\"userId\":\"$1\",\"purgeAt\":\"$t\"}}"
}
