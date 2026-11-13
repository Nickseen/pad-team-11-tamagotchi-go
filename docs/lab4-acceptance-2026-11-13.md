# Lab 4 acceptance receipt — 2026-11-13 (#165)

The complete Compose stack was rehearsed with the published images and released-main pins in
README's image table, starting from CPR dev `c4aa9de`. Domain modes used real services: Battle
`live`, Map/Raid/Guild dependency reads over gRPC. Two replicas of all eight domain services were
registered and ready. Existing data volumes were retained; local test users and domain objects
were created for the scenarios. Tokens, credentials and personal request bodies are omitted.

## Collections

Newman ran the existing collections against Gateway/Broker. The latest full Registry run includes
admin workflows; Raid used a real published package, guild, primary creature and raid definition.

| Collection | Requests | Assertions | Failures |
| --- | ---: | ---: | ---: |
| User Management | 48 | 90 | 0 |
| Package Registry | 36 | 60 | 0 |
| Guild | 42 | 82 | 0 |
| Map | 19 | 41 | 0 |
| Tamagotchi | 50 | 78 | 0 |
| Battle | 52 | 97 | 0 |
| Notification | 34 | 51 | 0 |
| Gateway logs | 5 | 6 | 0 |
| Message Broker | 19 | 43 | 0 |
| Monster Raid | 27 | 55 | 0 |
| **Total** | **332** | **603** | **0** |

Service Discovery's collection is explicitly a set of private-network examples, not a runner
suite: its mutation examples must not target live instances. Its status and ready registrations
were checked by `scripts/verify-lab4.mjs`; actual registration, removal and recovery occurred in
the controlled replica scenarios below. This receipt does not claim that collection was run as
a destructive batch.

## Runtime evidence by grade

| Grade | Observed evidence |
| --- | --- |
| 2–3 | Rust Broker source is present at released `cd194d5`; README links published-image CI/source pins. Existing README, diagram and collections describe the Broker topology. The full collection results are above. |
| 4 | Admin downloads of Guild warnings/errors and all components succeeded as NDJSON. The 1000/5000-line probes covered all 11 component labels. JWT-pattern scans of those downloaded samples found no matches; this is not a guarantee about every possible secret or log. |
| 5–6 | Real client requests crossed Gateway and Broker request queues; responses exposed upstream instance IDs. Broker topic inspection and real event consumers passed; runtime logs recorded `event.published`, `event.dead_lettered` and `event.replayed`. |
| 7 | Broker logs contained individual named `req-N` threads, plus `grpc-N` for forwarded RPCs. Topic-list inspection passed. |
| 8 | Broker `grpc.completed` recorded successful Guild GetMembership/ListMembers/GetGuild; UM ListUsers/GetRelationships; Tamagotchi ListTamagotchis/GetPrimary/GetSecondaryHolding/GetTypeAdvantage; Registry GetCurrentVersion/ListStatDefinitions/GetRaidDefinition; Raid GetParticipant. Unknown Raid returned the expected gRPC NotFound. Client internal-route rejection assertions passed. |
| 9 | With both Guild replicas stopped, a real participant's `user.deleted` event left `guild.events` depth 1. After Broker restart, depth remained 1. After Guild recovery, depth became 0 and memberCount changed 2 → 1. The Broker collection also listed/replayed a dead letter caused by an unsupported event version. |
| 10: failure | After an abrupt Guild-2 kill, all 40 parallel safe requests returned 200 through Guild-1. `broker_reroutes_total` changed 0 → 6 and `broker_circuit_trips_total` 0 → 1. Broker logged `request.rerouted`, `circuit.open`, `discovery.instance_removed`; SD logged `instance.removed`. Guild-2 was restored under a new instance ID. |
| 10: load | With both replicas idle, ten requests split 5/5. Guild-1 then reported activeTasks 60, maxTasks 64, ratio 0.9375; all ten new requests selected the idle Guild-2. |
| 10: monitoring | Prometheus reported 18 UP targets. Three DOWN targets were the optional, unstarted third Map/Guild/Raid replicas. Grafana health and the provisioned Message Broker, Domain services and Logs dashboards were available; Loki contained all 11 application components. |

After the failure/durability scenarios, the final preflight passed for Gateway/SD/Broker and
two ready replicas of every domain service. The normal stack remains running.

## Maximum log-download regression

The full-stack boundary probe found a deployment mismatch: Gateway accepts `limit=10000`, while
Loki's default maximum is 5000. Before correction, the existing collection's all-component request
at 10000 failed with Gateway 503; Loki returned 400 with
`max entries limit per query exceeded, limit > max_entries_limit (10000 > 5000)`.

`monitoring/loki/loki.yml` now explicitly sets `max_entries_limit_per_query: 10000`. Loki's
configuration verification passed and its container was recreated without deleting `loki-data`.
The same Postman assertion then passed, and a direct Loki query returned 200 with 10000 entries.
The existing collection retains the maximum-limit request as a regression check. Admin download,
non-admin rejection (403) and anonymous rejection (401) were also checked directly after the fix.
The four existing preflight tests and Postman JSON/script parsing checks passed.

## Publication gate

Technical rehearsal is complete. This receipt is not a main release or a substitute for review.
Merge the focused deployment fix into dev with one independent approval, then release CPR
`4.0.0` through `release/v4.0.0` → main with two independent approvals, resolved conversations and
passing required checks. Tag the actual main squash commit only after that merge. Keep #165 open
and the Project item out of Done until the tag is verified. The service image versions already
published in the README table are retained; this Loki configuration fix needs no application image
release or tag overwrite.
