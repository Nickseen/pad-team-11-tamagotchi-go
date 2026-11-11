#!/usr/bin/env bash
# Grade 9: high availability. guild-2 crashes; the Broker reroutes the requests that hit it to
# guild-1, its circuit for guild-2 opens after the failures, and Service Discovery removes that
# instance. Clients see no errors.
#
# The burst must be truly parallel to beat Service Discovery's 5 s probe (which would take guild-2
# out of rotation on its own), so it runs as 40 threads inside guild-1 rather than as shell jobs.
source "$(dirname "$0")/common.sh"
path="/api/v1/internal/guilds/$ZERO/members/$ZERO"

step "Before: requests spread over both Guild replicas"
for _ in $(seq 1 6); do queue_get guild "$path"; done | awk '{print "  " $1 " from instance " $2}'
trips_before="$(metric broker_circuit_trips_total)"
reroutes_before="$(metric broker_reroutes_total)"
since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

step "Crash guild-2 (docker compose kill: no clean deregistration)"
dc kill guild-2 >/dev/null 2>&1

step "40 parallel GET requests through the Broker right away (status, answering instance)"
dc exec -T guild-1 python -c "
import collections, os, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
url = 'http://message-broker:8091/api/v1/queues/guild$path'
def get(_):
    request = urllib.request.Request(url, headers={'X-Service-Token': os.environ['SERVICE_TOKEN']})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.headers.get('x-upstream-instance-id', '-')[:8]
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get('x-upstream-instance-id', '-')[:8]
with ThreadPoolExecutor(40) as pool:
    for (status, instance), count in sorted(collections.Counter(pool.map(get, range(40))).items()):
        print(f'  {count:3} x status {status} from instance {instance}')
"
sleep 8

step "What the Broker and Service Discovery did"
echo "  reroutes:      $reroutes_before -> $(metric broker_reroutes_total)   (broker_reroutes_total)"
echo "  circuit trips: $trips_before -> $(metric broker_circuit_trips_total)   (broker_circuit_trips_total)"
dc logs --no-log-prefix --since "$since" message-broker service-discovery 2>/dev/null |
  grep -oE '"event": ?"(request\.rerouted|circuit\.open|discovery\.instance_removed|instance\.removed)"' |
  sed 's/"event": *//' | sort | uniq -c | sed 's/^/  /' || true

step "Restore: guild-2 starts again as a new instance (a removed ID never comes back)"
dc up -d guild-2 >/dev/null 2>&1
echo "  guild-2 restarted; it registers under a new instance ID within ~10 s"
echo
echo "Expected: all 40 requests 200 from guild-1; reroutes and circuit trips went up; the Broker logged"
echo "request.rerouted, circuit.open and discovery.instance_removed; SD logged instance.removed."
