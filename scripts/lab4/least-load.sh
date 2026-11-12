#!/usr/bin/env bash
# Grade 9: the Broker routes by service load. guild-1 is made busy (its task slots fill up while
# its requests wait on a database lock); new requests then go to the idle guild-2.
source "$(dirname "$0")/common.sh"
SECONDS_BUSY=25

count() { # label
  local results; results="$(for _ in $(seq 1 10); do queue_get guild "/api/v1/internal/guilds/$ZERO/members/$ZERO"; done)"
  echo "$1:"; echo "$results" | awk '{print "  instance " $2}' | sort | uniq -c
}

step "Both Guild replicas idle: requests spread over both instances"
count "10 requests"

step "Make guild-1 busy for ${SECONDS_BUSY}s (lock its table, 60 slow requests inside guild-1)"
dc exec -d guild-db psql -U guild -d guild -c \
  "BEGIN; LOCK TABLE guilds IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep($((SECONDS_BUSY + 3))); COMMIT;"
dc exec -d guild-1 python -c "
import os, threading, time, urllib.request
end = time.time() + $SECONDS_BUSY
def hit():
    while time.time() < end:
        try:
            urllib.request.urlopen(urllib.request.Request('http://localhost:8087/api/v1/internal/guilds/$ZERO',
                headers={'X-Service-Token': os.environ['SERVICE_TOKEN']}), timeout=10)
        except Exception:
            pass
for _ in range(60): threading.Thread(target=hit).start()
"
echo "waiting 11 s for two Service Discovery probes to record guild-1's load…"
sleep 11
dc exec -T guild-1 python -c "
import json, urllib.error, urllib.request
try:
    body = json.load(urllib.request.urlopen('http://localhost:8087/api/v1/status'))
except urllib.error.HTTPError as error:
    body = json.load(error)
print('guild-1 status: ready', body['ready'], 'load', body['load'])"

step "guild-1 busy: the Broker sends new requests to the idle replica"
count "10 requests"
echo
echo "Expected: before, both instance IDs; while guild-1 is busy, (almost) all go to guild-2's ID."
