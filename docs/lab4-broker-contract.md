# Lab 4: Message Broker and gRPC contract

Status: **accepted** for CPR #151 on 2026-11-12. Nickseen, vladas9, gabimiric and
CyberMoonie confirmed the contract in [PR #167](https://github.com/Nickseen/pad-team-11-tamagotchi-go/pull/167),
which is merged. Acceptance specifies the interface; it does not certify unreleased images or
the complete shared-stack rehearsal. Implementations still require their own reviews and releases.

Protocol baseline: Message Broker dev commit 11d85d99c5d50ae4f8d972c09c3a35e853d19570.
The Broker README and proto at that commit define its current wire behavior.

## Topology and ownership

- Client REST: client -> Gateway -> Broker request queue -> selected domain replica.
- Domain RPC: service -> Broker gRPC listener -> selected domain replica.
- Events: producer outbox -> Broker topic -> one durable queue per subscribing service -> one ready consumer replica.
- WebSockets remain direct after Gateway ticket negotiation. Shared state/fan-out remains owned by each domain.
- Gateway retains JWT verification, trusted identity, client limits and deadlines. The Broker owns
  target selection, least-load balancing, physical-attempt breakers, SD failure reports and safe rerouting.
- Domain callers retain their logical dependency breakers but no longer report physical attempts to SD.
- SD observes/returns registrations and load. Broker/Gateway are not registered as routable domain targets.

Lab 3 behavior remains the baseline for unchanged 3.x deployments. This contract explicitly replaces
its no-command-retry rule only with the bounded, safe Broker rerouting rule below.

## Addresses and inspection access

| Component | Internal address |
| --- | --- |
| Broker HTTP | http://message-broker:8091 |
| Broker gRPC | message-broker:9091 |
| SD | http://service-discovery:8090 |
| Domain gRPC | HTTP port + 1000: user-management 9081 through monster-raid 9088 |

For local Postman/inspection, publish Broker HTTP on **127.0.0.1:8091**, outside Gateway.
Topic/dead-letter/replay APIs require SERVICE_TOKEN. This is an internal developer/admin interface,
not a public Gateway service slug. /metrics and /api/v1/status remain unauthenticated diagnostics.
A local-only 9091 mapping may be enabled for gRPC tooling; domain gRPC ports remain private.
Outside the local isolated network use authenticated encrypted transport, not exposed plaintext credentials.

## Authentication and request queues

Every Broker HTTP route except GET /api/v1/status and GET /metrics requires X-Service-Token,
compared in constant time. gRPC requires exactly one x-service-token metadata value.
All callers send a UUID X-Correlation-Id / x-correlation-id; propagate it across replies and events.
Error bodies use the existing CPR error envelope, including correlationId and UTC timestamp.

~~~text
ANY /api/v1/queues/{service}/{downstreamPath}
~~~

The service is an existing SD slug. downstreamPath starts with api/v1/. Method/query/body and
non-hop-by-hop headers pass through. Request deadline is 5000 ms from enqueue to reply,
bounded by an earlier propagated deadline where supported; domain task timeout remains independent.

**Token clarification:** pure service callers send SERVICE_TOKEN, never GATEWAY_TOKEN. Their
service token must reach every target path, including service-authenticated public endpoints.
For client traffic Gateway supplies X-Gateway-Token and trusted X-Authenticated-* headers;
the Broker removes the service token from that traffic on non-internal paths. It preserves
service authentication on internal paths. Services accept valid service-token calls without
a Gateway token, while client-context calls still require the Gateway credential.

Replies preserve target status/body and add:

- X-Upstream-Instance-Id;
- X-Upstream-Attempt-Id;
- X-Upstream-Task-Timeout-Ms;
- X-Broker-Queue-Wait-Ms.

These are control metadata for service callers. Gateway must not disclose trusted control
headers or credentials in public responses.

| Condition | HTTP result |
| --- | --- |
| Missing/invalid service token | 401 UNAUTHORIZED |
| Unknown service or invalid queue path | 404 ROUTE_NOT_FOUND |
| Queue full | 429 CONCURRENCY_LIMIT_EXCEEDED, Retry-After |
| No ready instance | 503 SERVICE_UNAVAILABLE, Retry-After: 1 |
| SD unavailable, all circuits open or unreachable target | 503 DEPENDENCY_UNAVAILABLE |
| Request deadline exhausted | 504 GATEWAY_TIMEOUT |
| Actual target response | Its own status and body |

## Events and delivery

Publish POST /api/v1/topics/{eventType}/messages with SERVICE_TOKEN, correlation ID and the
unchanged CPR envelope:

~~~json
{
  "eventId": "9f1c2b7e-3b2a-4c1d-9f31-2a7c5d0e4b11",
  "eventType": "map.players_nearby",
  "eventVersion": 1,
  "occurredAt": "2026-11-10T12:00:00.000Z",
  "producer": "map",
  "correlationId": "0b5f2a91-6d3c-4e8a-b0d2-9a1f7c4e6b33",
  "idempotencyKey": "f2c1b2a3-1111-4111-8111-111111111111",
  "payload": {}
}
~~~

Preserve existing payload field meanings. eventType must match the URL topic. The Broker returns
202 with messageId, topic and subscribers after durable storage; zero subscribers is valid.
Invalid envelope/topic returns 400 VALIDATION_FAILED; unavailable/uninitialized Broker returns 503.
Messages are at least once, not exactly once. Persist an outbox item in the same storage transaction
as the domain effect (Redis atomic script for Map; PostgreSQL transaction for Raid). Publish outside
the transaction and mark sent only after 202. Publication retries preserve eventId and payload.

| Topic | Producer | Consumers |
| --- | --- | --- |
| user.registered, user.friend_request_created, user.friend_request_accepted | User Management | Notification |
| user.deleted | User Management | Guild, Notification |
| tamagotchi.created, tamagotchi.leveled_up | Tamagotchi | none |
| tamagotchi.owner_transferred | Tamagotchi | Notification |
| registry.package_version_published | Package Registry | User Management |
| registry.raid_definition_activated | Package Registry | Monster Raid |
| battle.created | Battle | Notification |
| battle.finished | Battle | Tamagotchi, User Management, Notification |
| map.players_nearby | Map | Notification |
| guild.invitation_created, guild.member_joined, guild.member_left | Guild | Notification |
| raid.started, raid.expired | Monster Raid | Notification |
| raid.monster_defeated | Monster Raid | Tamagotchi, User Management, Notification |

Each subscriber service has one queue named <service>.events. Its replicas are competing
consumers; different services each receive their own copy. A known subscription survives
temporary absence of ready replicas, so queued events wait instead of being lost.

HTTP deliveries POST the original envelope to eventsPath with X-Service-Token,
X-Correlation-Id, X-Broker-Message-Id and X-Broker-Attempt. Acknowledge after local commit.
Validate authentication before parsing/applying data. Atomically record processed_events(event_id)
with the domain effect; duplicates return 200 with status duplicate. Unknown topics return
200 ignored. Unsupported higher event versions return 422 UNSUPPORTED_EVENT_VERSION.

| Delivery result | Broker action |
| --- | --- |
| 2xx | Acknowledge |
| 5xx, timeout, unreachable | Count physical failure; retry |
| 429 | Retry without breaker failure |
| Other 4xx, including unsupported version | Dead-letter immediately |
| No ready instance, all circuits open, SD unavailable | Wait without spending an attempt |

Default delivery policy: 5 attempts; initial backoff 1000 ms, doubled thereafter.
With DATA_DIR set, journals persist pending deliveries, retry state and dead letters.
Operator APIs: GET /api/v1/topics; GET /api/v1/dead-letters?queue=&offset=0&limit=50;
POST /api/v1/dead-letters/{id}/replay. Replay resets attempts after the cause is fixed;
unknown/already-replayed IDs return the Broker's documented not-found response.

## SD Lab 4 fields

Registration accepts these optional fields. Legacy registrations get the defaults below.

| Field | Validation/default |
| --- | --- |
| topics | Array of at most 50 dot-separated lowercase words/digits/underscores or whole-word * / # tokens; default [] |
| eventsPath | Canonical path below /api/v1/, no query/fragment/traversal; default /api/v1/internal/events |
| eventsTransport | http or grpc; default http |
| grpcAddress | host:port, no scheme/userinfo; optional, default null; mandatory for grpc event transport |

The deployment allowlist adds grpcAddresses per service. An address must be explicitly listed
and use the same hostname as that registration's baseUrl: it identifies the same replica.
Registration changes for an existing ID retain 409 INSTANCE_CONFLICT. Broker itself remains
an unknown domain service and cannot register as a routable target.

Every instance record/lookup also has load, initially null, then the most recent valid probe's
activeTasks, maxTasks and ratio. SD computes ratio from the counters. Registration cannot
override observed load; errors do not create fake zero-load measurements. Ready lookups still
exclude expired, unready and removed IDs. Existing lease/probe timings and tombstones remain.

Failure-report endpoint templates accept METHOD /api/v1/.../{id}, POST <eventsPath> and
GRPC /tamagotchi.<pkg>.v1.<Service>/<Method>. Physical attempts are reported by Broker.
Threshold remains three counted failures within ceil(target.taskTimeoutMs * 3.5), with attemptId
deduplication and monotonic elapsed windows. 429/business errors/caller cancellation do not count.

## gRPC schemas and authentication

Shared source root is CPR proto/. Copy Broker proto byte-for-byte from the pinned reference:
proto/tamagotchi/broker/v1/broker.proto. It defines Broker and EventSink.

Each owner contributes proto/tamagotchi/<pkg>/v1/<pkg>.proto. Package names use the SD slug with
hyphens replaced by underscores: tamagotchi.user_management.v1, tamagotchi.monster_raid.v1.
Declare one <Name>Internal service and distinct <Rpc>Request/<Rpc>Reply messages.
Use snake_case fields, UUIDs/UTC timestamps as strings, currency as int64 and the owner's
Go module path in go_package. Commit generated code or provide reproducible generation.
Broker proto has no Go option; Go consumers supply a protoc M mapping when generating it.

| Service | Internal RPCs relevant to this migration |
| --- | --- |
| Map | MapInternal.GetPosition: user_id -> position fields |
| Monster Raid | MonsterRaidInternal.GetParticipant: raid_id, user_id -> is_participant |
| User Management | Published in PR #166; GetRelationships, ListUsers, GetBalances, CheckBalance, AdjustBalance |
| Guild | Published by #169: GetGuild, GetMembership, ListMembers |
| Tamagotchi | Published by #172: ListTamagotchis, GetPrimary, GetSecondaryHolding, CreateSecondary, GrantXp, TransferOwner, GetTypeAdvantage |
| Package Registry | Published by #169: ListStatDefinitions, GetCurrentVersion, GetRaidDefinition |
| Battle, Notification | No domain internal RPC server required; implement dependency clients |

Map/Raid schemas accompany their own work. Dependency client interoperability requires the
upstream owner's published schema, not inferred protobuf field numbers from REST JSON.
Only own/service-token metadata authenticates internal RPCs; no user JWT or client-provided
identity substitutes for SERVICE_TOKEN. Enforce the same local concurrency slots/task budget
as REST. Reuse process-wide gRPC connections; drain servers during shutdown.

| REST/error | gRPC status |
| --- | --- |
| 400 validation | INVALID_ARGUMENT |
| 401 | UNAUTHENTICATED |
| 403 | PERMISSION_DENIED |
| 404 | NOT_FOUND |
| 409/422 | FAILED_PRECONDITION (the published UM mapping) |
| 429 | RESOURCE_EXHAUSTED |
| 503 dependency/task timeout | UNAVAILABLE / DEADLINE_EXCEEDED |
| 500 | INTERNAL |

Start the status description with the CPR error code; do not include raw dependency errors or credentials.
Broker forwards unary calls by package or x-target-service metadata. UNKNOWN, INTERNAL, UNAVAILABLE,
DEADLINE_EXCEEDED count as failures; RESOURCE_EXHAUSTED/business statuses do not. Its own queue
or discovery rejection must not create an extra physical-failure report.

EventSink is optional: services choosing grpc event delivery implement its Deliver RPC.
OK acknowledges, UNAVAILABLE/DEADLINE_EXCEEDED/INTERNAL/UNKNOWN retry with a breaker sample,
RESOURCE_EXHAUSTED retries without a sample, other statuses dead-letter.

## Safe rerouting and load balancing

Broker may move one failed HTTP attempt to a different instance only if no request reached the
first instance, the method is GET/HEAD/OPTIONS, or the body has a commandId that the target
deduplicates. Other state changes return the failure. gRPC refused connection/UNAVAILABLE may
reroute once; RPC owners must preserve idempotency for any state-changing method.

Use the higher of SD observed ratio and Broker local in-flight/maxTasks; without observed
load use in-flight counts. Equal scores use the Lab 3 round-robin successor rule.
Physical breakers are per instance/endpoint. SD quarantine cannot be undone by lease renewal;
recovery requires a fresh ID. Existing sockets are not migrated.

## Observability and rollout

Expose internal GET /metrics without user auth, bypassing task admission. Required domain
metrics: http_requests_total{route,method,status}, http_request_duration_seconds{route},
active_tasks, max_tasks, events_published_total{topic}, events_consumed_total{topic,status},
dependency_calls_total{service,code}. Labels are bounded route/topic/service values, never IDs.

Keep structured redacted JSON logs and correlation IDs. Downloadable system logs are #155;
shared Prometheus/Grafana deployment is #154; the full README/diagram/Postman update is #156.

Rollout order:
1. Review this contract and publish each owner's proto before dependent clients merge.
2. Prepare SD, Gateway and domain code in parallel with fake/real Broker integration tests.
3. Obtain reviewed 4.x.y service releases; update CPR pins only after actual main publication.
4. Integrate #154/#155/#156, then #165 verifies all PDF grades and prepares the CPR release.

Do not mark implementation/release evidence complete from stub tests or image publication alone.

## Accepted Raid reward-pool clarification

For #164, `rewardConfig.globalCurrency` and `rewardConfig.xp` are total integer pools.
Distribute over participants with positive damage, equally or by damage depending on
`distribution`. Use largest integer remainders; ties use ascending canonical userId.
This preserves every currency/XP unit, includes players who contributed before leaving,
and never pays zero-damage spectators. `damageShare` always describes actual damage.
Historical definitions without rewardConfig have zero pools. This clarification was accepted with the rest of the contract in PR #167. Raid emits the reward list; only UM/Tamagotchi apply their respective rewards.
