#!/usr/bin/env bash
# Grade 9: the dead letter channel. A user.deleted with eventVersion 2 is refused by Guild with
# 422 UNSUPPORTED_EVENT_VERSION, so the Broker dead-letters it at once. It is listed, then replayed.
source "$(dirname "$0")/common.sh"
member="$(uuid)"

step "Publish user.deleted with eventVersion 2 (Guild understands version 1)"
publish user.deleted "$(user_deleted "$member" 2)"; echo
sleep 3

step "GET /api/v1/dead-letters?queue=guild.events"
letters="$(broker_get "/api/v1/dead-letters?queue=guild.events&offset=0&limit=50")"
# The dead letter whose event is ours (parsed with the Python inside a service container).
read -r id reason <<<"$(echo "$letters" | dc exec -T guild-1 python -c "
import json, sys
for item in json.load(sys.stdin)['items']:
    if item['message']['body']['payload'].get('userId') == '$member':
        print(item['id'], item['reason'].replace(' ', '_'))
")"
echo "  dead letter $id"
echo "  reason: ${reason//_/ }"

step "Replay it (after a fix, e.g. a Guild release that understands version 2)"
if [ -n "$id" ]; then
  curl -s -X POST -H "X-Service-Token: $SERVICE_TOKEN" -H "X-Correlation-Id: $(uuid)" \
    "$BROKER/api/v1/dead-letters/$id/replay"; echo
  sleep 3
  echo "  Guild's answer to the replayed delivery:"
  dc logs --no-log-prefix --since 10s guild-1 guild-2 2>/dev/null |
    grep -oE '"event": "event\.(rejected|consumed)"[^}]*' | grep -oE '"(status|code|outcome)": "?[A-Z_a-z0-9]+' |
    paste -sd' ' | sed 's/^/  /' || true
fi
echo
echo "Expected: the event is listed with the 422 reason; replay sends it to Guild again. Guild still"
echo "answers 422 (it has not been upgraded), so it returns to the dead letter channel; after an"
echo "upgrade the replay would be applied."
