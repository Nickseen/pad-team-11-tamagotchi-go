#!/usr/bin/env bash
# Grade 8: durable queues. With every Guild replica stopped, a user.deleted event waits in the
# guild.events queue; it survives a Broker restart (journal in DATA_DIR) and is delivered once a
# Guild replica is back.
source "$(dirname "$0")/common.sh"

step "A guild with a member, created through the Broker"
read -r guild member owner <<<"$(guild_with_member)"
echo "  guild $guild, member $member, memberCount $(member_count "$guild" "$owner")"

step "Stop every Guild replica (the subscriber)"
dc stop guild-1 guild-2 >/dev/null 2>&1
echo "  guild-1 and guild-2 stopped"

step "User Management deletes the member: publish user.deleted"
publish user.deleted "$(user_deleted "$member")"; echo
sleep 2
echo "  guild.events depth: $(queue_depth guild.events)"

step "Restart the Broker: the queued event is on disk, not in memory"
dc restart message-broker >/dev/null 2>&1
for _ in $(seq 1 30); do curl -sf "$BROKER/api/v1/status" >/dev/null 2>&1 && break; sleep 1; done
echo "  guild.events depth after restart: $(queue_depth guild.events)   (broker_topic_queue_depth)"

step "Start Guild again: the event is delivered and applied"
dc start guild-1 guild-2 >/dev/null 2>&1
for _ in $(seq 1 40); do
  [ "$(member_count "$guild" "$owner" 2>/dev/null)" = "1" ] && break
  sleep 1
done
echo "  memberCount now $(member_count "$guild" "$owner") (the member was removed)"
dc logs --no-log-prefix --since 2m guild-1 guild-2 2>/dev/null |
  grep -oE '"event": "event\.consumed".*"outcome": "[a-z]+"' | grep -oE '"outcome": "[a-z]+"' | sort | uniq -c |
  sed 's/^/  guild /' || true
echo
echo "Expected: depth 1 while Guild is down, still 1 after the Broker restart, then 0; memberCount"
echo "2 -> 1; Guild logs event.consumed with outcome applied."
