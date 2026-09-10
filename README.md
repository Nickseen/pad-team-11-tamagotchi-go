# Tamagotchi Go

Backend-as-a-Service ecosystem for third-party Tamagotchi apps ("packages"). Each package ships its
own creatures, art and local growth mechanics, while a shared backend lets users from different
packages meet, battle, trade creatures, form guilds and fight cooperative monster raids.

---

## Table of Contents

- [Service Boundaries](#service-boundaries)
- [Architecture Diagram](#architecture-diagram)
- [Technologies and Communication Patterns](#technologies-and-communication-patterns)
- [Communication Overview](#communication-overview)
- [Communication Contract](#communication-contract)
- [Open Boundary Decisions](#open-boundary-decisions)
- [Contribution Workflow](#contribution-workflow)

---

## Service Boundaries

Eight microservices. Each one owns a single slice of state and is the **only** writer of that slice;
everything else reads it through an API or reacts to its events.

| # | Service          | Owns (single source of truth)                                                              | Explicitly does **not** own                               |
| - | ---------------- | ------------------------------------------------------------------------------------------ | -------------------------------------------------------- |
| 1 | User Management  | accounts, credentials, friends/enemies, local + global currency balances                   | creature state, battle math, geolocation                 |
| 2 | Tamagotchi       | creature entities, owner reference, combat type, level, sprites, raw package-local stats   | interpretation of those stats, damage formulas, currency |
| 3 | Package Registry | packages, versions, moderators/admins, stat *definitions*, monster & raid *definitions*    | user identity, live raid state                           |
| 4 | Battle           | PvP match runtime: state, turns, damage, outcome                                           | creature ownership record, currency balances             |
| 5 | Map              | latest known coordinates per user, proximity detection                                     | notification delivery, battle creation                   |
| 6 | Notification     | device tokens, delivery preferences, push dispatch                                         | any domain state whatsoever                              |
| 7 | Guild            | guilds, membership, roles, permissions, guild chat messages                                | raid mechanics, user identity                            |
| 8 | Monster Raid     | raid runtime: monster HP, participants, damage log, status                                 | monster definitions, guild membership, currency          |

### 1. User Management Service

The authority for identity. Answers three questions for the whole system: *who is this user*,
*are these two users friends or enemies*, and *does this user have enough global currency*.

Stores registration data (username, password hash, email), the social graph, and both currencies —
local currency, whose value and acquisition are package-specific, and global currency, shared across
the ecosystem and earned mainly through battles and raids.

It never computes rewards. Battle and Raid tell it *what happened*; it applies the balance change.

### 2. Tamagotchi Service

Owns every creature in the ecosystem. A user has one **primary** Tamagotchi, provided by the package
they installed, and may acquire **secondary** ones originating from other users and packages.

Secondary Tamagotchis are **references to existing entities, never copies** — copying would let level
and stats diverge between the original and the reference.

Local health statistics (hunger, tiredness, happiness, energy, discipline, …) are stored as an
opaque, non-normalized document. This service persists them but does not know what they mean.

Combat types form a fixed advantage ring:

```
Flame → Nature → Earth → Electric → Water → Shadow → Flame
```

### 3. Package Registry Service

The configuration service of the ecosystem. Stores package identity, version, description, status
and associated developers.

**Moderators** are package-scoped privileged users who define their package's local growth mechanics
and the *interpretation rules* for its statistics — maximum values, and thresholds that grant a
combat bonus. This is what makes non-normalized stats usable: Battle asks the Registry how to read a
number instead of forcing every package into a shared schema.

**Admins** are globally privileged and design monster raids: monster name, sprites, max HP, combat
stats, weaknesses, resistances, duration, participant limits and reward configuration.

### 4. Battle Service

Executes turn-based PvP after a match is created. Each player picks a primary and a secondary
Tamagotchi and may equip battle boosts.

Damage is derived from primary/secondary level, type advantage, equipped boosts and current health
stats. Starting health derives from Tamagotchi levels.

On completion: the winner receives global currency, XP and **the loser's primary Tamagotchi**; the
loser loses global currency and receives a smaller amount of XP. XP is split between primary and
secondary by a fixed rule (60/40).

The ownership transfer is not written by this service. Battle publishes the outcome; Tamagotchi
reassigns the owner and User Management adjusts balances, both keyed on `battleId` for idempotency.

### 5. Map Service

Ingests a continuous stream of geolocation updates, keeps only the latest coordinate and timestamp
per user, and discards stale locations.

Friends and enemies are always visible on the map. Unknown users become relevant only within a
proximity threshold of roughly 6 metres. When two previously unrelated users cross that threshold,
the service emits a proximity event.

It does not create battles and does not send notifications — it only reports that two people are
near each other.

### 6. Notification Service

A pure subscriber. Other services publish domain events; this service decides how each event reaches
the client and dispatches it through Firebase push.

Delivers: friend request received, nearby player detected, battle request received, another player
used or captured a Tamagotchi, guild invitation, raid started.

### 7. Guild Service

Owns guild identity, membership, roles and permissions (owner/leader, officers, members), and
provides real-time **Guild Chat** over WebSockets — messages carry a guild, an author and a
timestamp.

Guilds are the social context for Monster Raids: members join an active raid and their primary
Tamagotchis become participants. Membership and invitation rules resolve identity and relationships
through User Management.

### 8. Monster Raid Service

Runs cooperative clicker-style raids in which a guild collectively fights one monster with a large
HP pool and a fixed duration.

Any eligible guild member contributes their primary Tamagotchi. Every participant deals damage to
the same monster; the service maintains current monster HP, participants, damage dealt, timestamps
and raid status. A raid fails when its timer expires.

Because the whole guild hits one counter concurrently, HP decrements must be atomic, and reward
distribution must fire exactly once per raid.

---

## Architecture Diagram

Each box lists the state its service exclusively owns. Solid arrows are synchronous requests, dashed
arrows are asynchronous events. Arrows point from the caller toward the owner of the data.

![Architecture](docs/architecture.svg)

Two properties are worth pointing out, because they are the reason the boundaries are drawn this
way:

**The dependency graph is acyclic.** Nothing in the core layer calls back up into a gameplay
service. Battle reads from Tamagotchi, Registry and User Management, but none of them knows Battle
exists — they learn about a finished fight from an event, not a call.

**Nobody writes to state they do not own.** Battle decides who won, but Tamagotchi performs the
owner transfer and User Management applies the currency change, both keyed on `battleId` so a
redelivered event cannot hand over the same creature twice.

---

## Technologies and Communication Patterns

The implementation deliberately uses exactly **two languages: Go and Python**. Go is assigned to
the services whose load is dominated by concurrent requests, timers or latency-sensitive state
transitions. Python with FastAPI is assigned to data-oriented services where flexible JSON models,
validation and third-party SDK integration matter more than raw throughput. Splitting the services
four-to-four gives the team meaningful experience in both stacks without introducing a third
language or a unique stack for every service.

### Shared infrastructure and contracts

- **Synchronous service APIs — REST over HTTP with JSON.** REST is easy to inspect and has mature
  support in both Go and FastAPI. OpenAPI documents the request/response schemas and generates
  clients, which reduces mistakes at the language boundary. The trade-off is more payload and less
  compile-time coupling than gRPC; for this business case, interoperability with third-party
  Tamagotchi packages and debuggability are more valuable than a small serialization gain.
- **Asynchronous domain events — RabbitMQ topic exchanges with durable queues.** Producers publish
  facts such as `BattleFinished`, `PlayersNearby` and `RaidStarted`; every interested service owns a
  separate queue. Delivery is at least once, so consumers acknowledge only after committing their
  local transaction, retry transient failures, route poison messages to a dead-letter queue and
  deduplicate by `eventId`. This adds eventual consistency and broker operations, but prevents a
  slow push provider or reward handler from blocking gameplay and lets several services react to
  one result independently.
- **Live client updates — WebSockets.** They are reserved for high-frequency, bidirectional or
  server-pushed data: map updates, guild chat and the raid feed. A socket costs more operationally
  than stateless HTTP and needs reconnect/heartbeat handling, but polling would waste bandwidth and
  make these features feel delayed.
- **Data ownership — database per service.** PostgreSQL is the durable default, with a separate
  database/schema and credentials for each service; no service reads another service's tables.
  Redis is an internal accelerator only where explicitly listed. This duplicates some data and
  requires events to propagate changes, but preserves independent deployment and prevents one
  service from bypassing another service's business rules.

### Service-by-service selection

| Service | Language and storage | Communication patterns | Motivation and trade-offs |
| ------- | -------------------- | ---------------------- | ------------------------- |
| **User Management** | **Go**, PostgreSQL | REST/JSON for registration, login, JWT validation, relationships and balance queries. Publishes social events; consumes battle/raid results to apply currency changes idempotently. | Authentication and balance writes need predictable latency, strict types and safe concurrency. Go produces a small deployable binary and handles many simultaneous sessions cheaply. It is more verbose than Python and schema evolution requires more explicit code, which is acceptable for security-sensitive, stable identity contracts. |
| **Battle** | **Go**, PostgreSQL; Redis for short-lived match state and command deduplication | REST/JSON commands for creating/joining a battle and submitting a turn. Synchronous REST reads from User Management, Tamagotchi and Package Registry before damage calculation. Publishes `BattleFinished`; no distributed database writes. | Goroutines fit many independent battles, while static types make damage and reward rules explicit. Redis makes turn access fast, but adds cache/state coordination; PostgreSQL remains the durable record so a Redis restart cannot decide a match outcome. Events keep ownership transfer, rewards and notifications off the critical response path. |
| **Map** | **Go**, Redis GEO with TTL; PostgreSQL only for durable configuration/audit data | WebSocket for the client's location stream and nearby-player updates. Synchronous REST checks relationships in User Management. Publishes `PlayersNearby` only when users cross the proximity boundary. | Location traffic is frequent, concurrent and ephemeral. Go keeps long-lived connections affordable, and Redis GEO provides proximity queries plus natural expiry of stale positions. Redis is not the durable source of user data; accepting that locations may disappear after failure is consistent with the business rule that stale positions must be discarded anyway. |
| **Monster Raid** | **Go**, PostgreSQL; Redis atomic operations for the live HP counter and timers | REST/JSON to create/join/attack; synchronous reads from Guild, Tamagotchi and Package Registry. WebSocket broadcasts HP and participant damage. Publishes `RaidStarted` and one terminal `MonsterDefeated`/`RaidExpired` event. | A guild can hit one counter concurrently, so Go's concurrency model and atomic Redis operations suit the hot path. Durable snapshots and an idempotent terminal transition in PostgreSQL prevent rewards from firing twice. The dual-store design is more complex, but isolates high-frequency damage from durable history. |
| **Tamagotchi** | **Python 3**, FastAPI, Pydantic, PostgreSQL JSONB | REST/JSON for creature CRUD and stat lookup. Consumes `BattleFinished` to transfer ownership and apply XP once; publishes creature lifecycle events used by Notification. | Package-local stats are intentionally non-normalized. Python handles evolving dictionaries naturally, while Pydantic validates the stable envelope around the JSONB payload. This is less compile-time-safe and slower than Go, so validation is mandatory and CPU-heavy battle calculations stay in Battle. |
| **Notification** | **Python 3**, FastAPI, Pydantic, PostgreSQL; Firebase Admin SDK | Primarily a RabbitMQ subscriber. It consumes social, proximity, battle, creature, guild and raid events and calls Firebase Cloud Messaging; REST is limited to device-token and preference management. | Notification delivery is I/O-bound, and Python has a maintained Firebase Admin SDK plus rapid integration code. Broker queues absorb Firebase latency and outages. The cost is eventual delivery and Python's lower CPU throughput, neither of which is critical because notifications do not decide domain outcomes. |
| **Guild** | **Python 3**, FastAPI, Pydantic, PostgreSQL | REST/JSON for guild, membership, role and invitation CRUD. WebSocket rooms carry live chat. Synchronous REST validates users through User Management; publishes `GuildInvitation` and membership-change events. | Most work is validated CRUD, for which FastAPI and Pydantic minimize boilerplate; its built-in WebSocket support covers chat without another stack. Each process needs a broker-backed fan-out when scaled horizontally, so RabbitMQ carries room messages between instances while PostgreSQL keeps chat history. |
| **Package Registry** | **Python 3**, FastAPI, Pydantic, PostgreSQL JSONB | REST/JSON/OpenAPI for package versions, stat definitions and monster/raid definitions. Publishes versioned configuration-change events so consumers can invalidate caches. | Definitions vary between third-party packages, making Pydantic discriminated models and JSONB more adaptable than rigid Go structs. That flexibility can hide incompatible changes, so definitions are immutable by version and validated on write; runtime services request an explicit version rather than silently taking the latest one. |

The language choice follows the workload rather than organizational convenience: **Go** owns the
hot concurrent paths (identity traffic, battles, geolocation and raid counters), while
**Python/FastAPI** owns flexible schemas, CRUD-heavy domains and Firebase integration. REST/JSON is
the common synchronous boundary, RabbitMQ carries durable cross-domain facts, and WebSockets are
used only when continuous client updates justify their connection-management cost.

### Persistent connections

Everything else is request/response. Only these three client channels stay open:

| Channel    | Service      | Carries                                    |
| ---------- | ------------ | ------------------------------------------ |
| Live map   | Map          | location updates and nearby-player changes |
| Guild chat | Guild        | member messages within a guild room        |
| Raid feed  | Monster Raid | live monster HP and per-participant damage |

---

## Communication Overview

**Synchronous** where a decision cannot proceed without the answer — Battle cannot compute damage
without creature stats and their package interpretation; Raid cannot admit a player without
confirming guild membership.

**Asynchronous** where the producer does not care who reacts, or where several services must react
to the same fact. `BattleFinished` is consumed by Tamagotchi (owner transfer and XP), User
Management (currency) and Notification (push) independently.

**WebSockets** for sustained client connections: live map updates, guild chat and live raid damage.

Every event consumer is idempotent on the event id (`battleId`, `raidId`), so a redelivered or
duplicated event cannot transfer the same creature twice or pay out a raid twice.

## Communication Contract

This section is the normative contract between the eight services. It defines how data is managed
across the system, every endpoint each service exposes, the payload transferred in each direction
with its format and types, and the response returned. Anything not listed here is not part of the
contract and may not be relied upon by another service.

### Transport and conventions

| Concern | Rule |
| ------- | ---- |
| Synchronous transport | HTTP/1.1 with TLS; `Content-Type: application/json; charset=utf-8` in both directions |
| Path prefix | `/api/v1` on every service; the major number changes only on an incompatible change |
| Internal-only routes | Prefixed `/api/v1/internal`; reachable from the service network, never from clients |
| Live channels | WebSocket, JSON text frames, one JSON object per frame |
| Asynchronous transport | RabbitMQ topic exchange `tamagotchi.events`, durable queues, at-least-once delivery |
| Time | RFC 3339 with an explicit `Z` offset, always UTC, millisecond precision |
| Identifiers | UUID v4 rendered as a lowercase canonical string |
| Field naming | `camelCase` in JSON, regardless of the language implementing the service |
| Unknown fields | Consumers ignore unknown fields; producers never remove or retype a field within a major version |

### Type vocabulary

These names are used in every payload table below.

| Name | JSON type | Definition |
| ---- | --------- | ---------- |
| `UUID` | string | UUID v4, lowercase canonical form, e.g. `9f1c2b7e-3b2a-4c1d-9f31-2a7c5d0e4b11` |
| `Timestamp` | string | RFC 3339 UTC, e.g. `2026-09-10T14:25:31.482Z` |
| `Currency` | integer | Signed 64-bit amount in the smallest indivisible unit; never a floating-point number |
| `CombatType` | string | One of `flame`, `nature`, `earth`, `electric`, `water`, `shadow` |
| `Stats` | object | Package-local statistics; opaque `string → number` map, not normalized across packages |
| `SemVer` | string | `MAJOR.MINOR.PATCH`, e.g. `1.4.2` |
| `Coordinate` | number | Decimal degrees, WGS 84; latitude in `[-90, 90]`, longitude in `[-180, 180]` |
| `Cursor` | string | Opaque, service-generated pagination token; clients must not parse it |

### Authentication and authorization

Clients authenticate against User Management and receive a signed JWT. Every other service
validates that token **locally** using the public keys published at
`GET /api/v1/.well-known/jwks.json`, so a request never costs an extra round trip to User
Management. Token claims:

```json
{
  "sub": "9f1c2b7e-3b2a-4c1d-9f31-2a7c5d0e4b11",
  "iss": "user-management",
  "aud": "tamagotchi-go",
  "packageId": "1d4e9a02-8c77-4a1e-b6f0-77c3b1d9e402",
  "roles": ["user", "moderator"],
  "iat": 1789041931,
  "exp": 1789045531,
  "jti": "0b5f2a91-6d3c-4e8a-b0d2-9a1f7c4e6b33"
}
```

Client requests carry `Authorization: Bearer <accessToken>`. Service-to-service calls carry both
that header and `X-Service-Token`, a short-lived credential identifying the calling service; routes
under `/api/v1/internal` require it. Every request also carries `X-Correlation-Id: UUID`, which is
propagated through synchronous calls and copied into any event the request produces.

### Error envelope

Every non-2xx response from every service has exactly this shape:

```json
{
  "error": {
    "code": "TAMAGOTCHI_NOT_OWNED",
    "message": "Tamagotchi 4f2c… is not owned by the requesting user.",
    "details": { "tamagotchiId": "4f2c8e1a-…", "ownerId": "77b1c0de-…" }
  },
  "correlationId": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
  "timestamp": "2026-09-10T14:25:31.482Z"
}
```

`code` is a stable `SCREAMING_SNAKE_CASE` string and is part of the contract; `message` is
human-readable and is not. `details` is an object or `null`.

| Status | Meaning in this system |
| ------ | ---------------------- |
| `400 Bad Request` | Malformed JSON or a value outside its documented range |
| `401 Unauthorized` | Missing, expired or invalid token |
| `403 Forbidden` | Authenticated but not permitted — wrong owner, role or guild |
| `404 Not Found` | The addressed resource does not exist or is not visible to the caller |
| `409 Conflict` | The request contradicts current state, e.g. joining a finished raid |
| `422 Unprocessable Entity` | Schema-valid but domain-invalid, e.g. an unknown `CombatType` |
| `429 Too Many Requests` | Rate limit exceeded; `Retry-After` is set |
| `500 Internal Server Error` | Unexpected failure; the correlation id is required when reporting it |
| `503 Service Unavailable` | A required downstream dependency is unreachable |

### Collections and pagination

Every collection endpoint accepts `limit` (integer, `1…100`, default `20`) and `cursor` (`Cursor`,
optional) and returns:

```json
{ "items": [ /* resource objects */ ], "nextCursor": "Y3JlYXRlZEF0OjIwMjYtMDktMTBUMTQ6MjU6MzEuNDgyWnxpZDo5ZjFjMmI3ZQ==", "hasMore": true }
```

`nextCursor` is `null` when `hasMore` is `false`. The cursor is base64 of the sort position of
the last item returned — the example above decodes to
`createdAt:2026-09-10T14:25:31.482Z|id:9f1c2b7e` — but it is opaque by contract, so the encoding
can change without breaking a client.

### Idempotency

Two distinct mechanisms, because two distinct problems exist.

**Client-issued commands** that change state and may be retried over a flaky connection — a battle
turn, a raid attack, a balance adjustment — take a client-generated `commandId` (`UUID`) in the
body. The service stores it with the result for at least 24 hours. A repeat of the same
`commandId` returns the **original** result with `200 OK` instead of applying the change twice.

**Event consumers** deduplicate on the envelope's `eventId`, which is recorded in a `processed_events`
table inside the same local transaction that applies the effect. Because delivery is at-least-once,
this is what makes it exactly-once in effect: a redelivered `battle.finished` cannot transfer the
same creature twice or pay a raid reward twice.

### Data management across services

Each service owns a private datastore. **No service opens a connection to another service's
database** — each one has its own credentials, its own schema and its own migrations, and is the
only writer of the state listed under [Service Boundaries](#service-boundaries). Data crosses a
boundary in exactly one of two ways: a synchronous REST read, or an asynchronous event.

| Service | Primary store | Owned tables / keys | Volatile store |
| ------- | ------------- | ------------------- | -------------- |
| User Management | PostgreSQL `usermgmt` | `users`, `credentials`, `refresh_tokens`, `relationships`, `friend_requests`, `balances`, `balance_operations` | — |
| Tamagotchi | PostgreSQL `tamagotchi` | `tamagotchis`, `secondary_references`, `stat_documents` (JSONB), `xp_operations` | — |
| Package Registry | PostgreSQL `registry` | `packages`, `package_versions`, `stat_definitions` (JSONB), `package_registrations`, `monsters`, `raid_definitions` | — |
| Battle | PostgreSQL `battle` | `battles`, `battle_turns`, `battle_participants`, `processed_commands` | Redis: `battle:{battleId}:state`, TTL 1 h |
| Map | Redis (authoritative for positions) | `geo:users` (GEO set), `loc:{userId}` hash, TTL 5 min | PostgreSQL `map` for `proximity_audit` only |
| Notification | PostgreSQL `notification` | `devices`, `preferences`, `notifications`, `processed_events` | — |
| Guild | PostgreSQL `guild` | `guilds`, `memberships`, `invitations`, `chat_messages` | Redis Pub/Sub `guild:{guildId}:chat` for cross-instance fan-out |
| Monster Raid | PostgreSQL `raid` | `raids`, `raid_participants`, `damage_log`, `processed_commands` | Redis: `raid:{raidId}:hp` counter, `raid:{raidId}:timer` |

**Duplicated data is a cached projection, never a second source of truth.** Where a service keeps a
foreign identifier — Guild storing `userId`, Raid storing `tamagotchiId` — it stores the identifier
only and resolves the rest through the owner's API. Where it keeps a denormalized copy for display,
the copy is refreshed from the owning service's events and is never used for an authorization or
money decision.

Three ownership rules follow directly from
[Open Boundary Decisions](#open-boundary-decisions) and are binding on the contract below:

1. **Package Registry** writes the user ↔ package link; User Management reads it.
2. **Tamagotchi** writes the owner of a creature. Battle never does — it publishes `battle.finished`
   and Tamagotchi performs the transfer.
3. **User Management** writes every balance. Battle and Raid compute rewards but publish them as
   facts; the balance change is applied by the owner of the balance.

Consistency across services is therefore **eventual**, bounded by broker latency. Consistency
*within* a service is transactional: a consumer applies the effect and records the `eventId` in the
same PostgreSQL transaction, so an at-least-once redelivery is absorbed rather than duplicated.

**Referential integrity** cannot be enforced with foreign keys across services. Instead, a service
validates a foreign identifier synchronously at write time (Guild asks User Management whether a
user exists before creating a membership) and reacts to deletion events afterwards. A dangling
reference is treated as a soft failure — the resource renders as `unavailable` rather than crashing
the request.

### Endpoint reference

Every endpoint below is listed with the data it transfers in each direction. Request bodies are
JSON unless the row says otherwise; path and query parameters are marked as such. `Auth` is `—` for
public routes, `user` for a client JWT, `role` for a JWT carrying that role, and `service` for the
internal routes that additionally require `X-Service-Token`.

Resource objects are defined once per service and referenced by name from the endpoint tables. A
response cell naming an object means that object is the entire response body; `[Object]` means a
paginated collection of it, wrapped in the `items`/`nextCursor`/`hasMore` envelope.

#### 1. User Management Service — `http://user-management:8081`

**`User`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `userId` | `UUID` | |
| `username` | string | 3–32 characters, `^[a-zA-Z0-9_]{3,32}$`, unique |
| `email` | string | RFC 5322; returned only to the owner of the account |
| `displayName` | string \| null | ≤ 64 characters |
| `avatarRef` | string \| null | Asset reference resolved by the client's package |
| `status` | string | `active`, `suspended` or `deleted` |
| `createdAt` | `Timestamp` | |

**`Balances`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `userId` | `UUID` | |
| `globalCurrency` | `Currency` | Never negative |
| `localBalances` | array | `[{ "packageId": UUID, "amount": Currency }]`, one entry per registered package |
| `updatedAt` | `Timestamp` | |

**`Relationship`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `userId` | `UUID` | The *other* user |
| `relation` | string | `friend`, `enemy` or `none` |
| `since` | `Timestamp` \| null | `null` when `relation` is `none` |

**`FriendRequest`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `requestId` | `UUID` | |
| `fromUserId` | `UUID` | |
| `toUserId` | `UUID` | |
| `status` | string | `pending`, `accepted`, `declined` or `cancelled` |
| `createdAt` | `Timestamp` | |

##### Authentication

| Method and path | Auth | Request | Response |
| --------------- | ---- | ------- | -------- |
| `POST /api/v1/auth/register` | — | `username` string, `email` string, `password` string (≥ 10 chars), `packageId` `UUID` | `201` → `User` |
| `POST /api/v1/auth/login` | — | `username` string, `password` string | `200` → `TokenPair` |
| `POST /api/v1/auth/refresh` | — | `refreshToken` string | `200` → `TokenPair` |
| `POST /api/v1/auth/logout` | user | `refreshToken` string | `204` → empty body |
| `GET /api/v1/.well-known/jwks.json` | — | — | `200` → `{ "keys": [JWK] }`, cacheable for 1 h |

`TokenPair` is `{ "accessToken": string, "refreshToken": string, "tokenType": "Bearer",
"expiresIn": integer (seconds), "userId": UUID }`. Access tokens live 1 hour, refresh tokens 30
days. Errors: `401 INVALID_CREDENTIALS`, `409 USERNAME_TAKEN`, `409 EMAIL_TAKEN`,
`422 WEAK_PASSWORD`.

##### Profiles

| Method and path | Auth | Request | Response |
| --------------- | ---- | ------- | -------- |
| `GET /api/v1/users/me` | user | — | `200` → `User` including `email` |
| `PATCH /api/v1/users/me` | user | any of `displayName` string, `avatarRef` string, `email` string | `200` → `User` |
| `GET /api/v1/users/{userId}` | user | path `userId` `UUID` | `200` → `User` without `email` |
| `GET /api/v1/users` | service | query `ids` — comma-separated `UUID`, ≤ 100 | `200` → `{ "items": [User] }` for bulk resolution |
| `DELETE /api/v1/users/me` | user | `password` string | `202` → `{ "status": "scheduled", "purgeAt": Timestamp }` |

##### Relationships

| Method and path | Auth | Request | Response |
| --------------- | ---- | ------- | -------- |
| `GET /api/v1/users/me/friends` | user | query `limit`, `cursor` | `200` → `[User]` |
| `POST /api/v1/users/me/friend-requests` | user | `targetUserId` `UUID` | `201` → `FriendRequest`; publishes `user.friend_request_created` |
| `GET /api/v1/users/me/friend-requests` | user | query `direction` = `incoming` \| `outgoing`, `status`, `limit`, `cursor` | `200` → `[FriendRequest]` |
| `POST /api/v1/users/me/friend-requests/{requestId}/accept` | user | path `requestId` `UUID` | `200` → `FriendRequest` with `status: "accepted"`; publishes `user.friend_request_accepted` |
| `POST /api/v1/users/me/friend-requests/{requestId}/decline` | user | path `requestId` `UUID` | `200` → `FriendRequest` with `status: "declined"` |
| `DELETE /api/v1/users/me/friends/{userId}` | user | path `userId` `UUID` | `204` → empty body |
| `GET /api/v1/users/me/enemies` | user | query `limit`, `cursor` | `200` → `[User]` |
| `POST /api/v1/users/me/enemies` | user | `targetUserId` `UUID` | `201` → `Relationship` |
| `DELETE /api/v1/users/me/enemies/{userId}` | user | path `userId` `UUID` | `204` → empty body |
| `GET /api/v1/internal/relationships` | service | query `userId` `UUID`, `otherUserIds` — comma-separated `UUID`, ≤ 100 | `200` → `{ "items": [Relationship] }` |

`GET /api/v1/internal/relationships` is the hot path for Map, which calls it on every proximity
evaluation to decide whether a nearby user is a friend, an enemy or a stranger. It is a pure read
and safe to cache for 30 seconds.

##### Balances

| Method and path | Auth | Request | Response |
| --------------- | ---- | ------- | -------- |
| `GET /api/v1/users/me/balances` | user | — | `200` → `Balances` |
| `GET /api/v1/internal/users/{userId}/balances` | service | path `userId` `UUID` | `200` → `Balances` |
| `GET /api/v1/internal/users/{userId}/balances/check` | service | query `currency` = `global` \| `local`, `packageId` `UUID` (required when `local`), `amount` `Currency` | `200` → `{ "sufficient": boolean, "available": Currency }` |
| `POST /api/v1/internal/users/{userId}/balances/adjust` | service | `commandId` `UUID`, `currency` string, `packageId` `UUID` \| null, `delta` `Currency`, `reason` string | `200` → `Balances`; idempotent on `commandId` |

`delta` is signed. A debit that would drive `globalCurrency` below zero is rejected with
`409 INSUFFICIENT_FUNDS` and the balance is unchanged. The synchronous adjust route exists for
flows that must fail fast — a shop purchase — while battle and raid rewards arrive as events.

#### 2. Tamagotchi Service — `http://tamagotchi:8082`

**`Tamagotchi`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `tamagotchiId` | `UUID` | |
| `ownerId` | `UUID` | Current owner; changes only through `battle.finished` |
| `originPackageId` | `UUID` | The package that created the creature; never changes |
| `name` | string | 1–32 characters |
| `combatType` | `CombatType` | Immutable after creation |
| `level` | integer | ≥ 1 |
| `xp` | integer | ≥ 0; resets to the level threshold on level-up |
| `spriteRef` | string | Asset reference resolved by the owning package |
| `stats` | `Stats` | Package-local, non-normalized; this service does not interpret it |
| `statsVersion` | integer | Optimistic-concurrency counter, incremented on every stat write |
| `capturedInBattleId` | `UUID` \| null | Set when the creature changed hands after a battle |
| `createdAt` / `updatedAt` | `Timestamp` | |

**`SecondaryReference`** — a *reference* to an existing `Tamagotchi`, never a copy.

| Field | Type | Notes |
| ----- | ---- | ----- |
| `referenceId` | `UUID` | |
| `holderId` | `UUID` | The user who may field the creature as a secondary |
| `tamagotchiId` | `UUID` | Points at the original entity, whose level and stats stay shared |
| `acquiredFrom` | string | `battle`, `trade` or `capture` |
| `acquiredAt` | `Timestamp` | |

##### Creatures

| Method and path | Auth | Request | Response |
| --------------- | ---- | ------- | -------- |
| `POST /api/v1/tamagotchis` | user | `packageId` `UUID`, `name` string, `combatType` `CombatType`, `spriteRef` string, `stats` `Stats` | `201` → `Tamagotchi`; publishes `tamagotchi.created` |
| `GET /api/v1/tamagotchis/{tamagotchiId}` | user | path `tamagotchiId` `UUID` | `200` → `Tamagotchi` |
| `GET /api/v1/tamagotchis` | user | query `ownerId` `UUID`, `packageId` `UUID`, `combatType`, `limit`, `cursor` | `200` → `[Tamagotchi]` |
| `GET /api/v1/internal/tamagotchis` | service | query `ids` — comma-separated `UUID`, ≤ 50 | `200` → `{ "items": [Tamagotchi] }` |
| `PATCH /api/v1/tamagotchis/{tamagotchiId}` | user | `name` string, `spriteRef` string | `200` → `Tamagotchi` |
| `DELETE /api/v1/tamagotchis/{tamagotchiId}` | user | path `tamagotchiId` `UUID` | `204`; refused with `409 TAMAGOTCHI_IS_PRIMARY` if it is the owner's primary |

`GET /api/v1/internal/tamagotchis` is the bulk read Battle and Monster Raid perform before every
damage calculation; it is the reason the route is batched rather than one call per creature.

##### Primary and secondary assignment

| Method and path | Auth | Request | Response |
| --------------- | ---- | ------- | -------- |
| `GET /api/v1/users/{userId}/primary-tamagotchi` | user | path `userId` `UUID` | `200` → `Tamagotchi`; `404 NO_PRIMARY_TAMAGOTCHI` if unset |
| `PUT /api/v1/users/me/primary-tamagotchi` | user | `tamagotchiId` `UUID` | `200` → `Tamagotchi`; `403 TAMAGOTCHI_NOT_OWNED` if the caller is not the owner |
| `GET /api/v1/users/{userId}/secondary-tamagotchis` | user | path `userId` `UUID`, query `limit`, `cursor` | `200` → `[SecondaryReference]` |
| `POST /api/v1/internal/users/{userId}/secondary-tamagotchis` | service | `commandId` `UUID`, `tamagotchiId` `UUID`, `acquiredFrom` string | `201` → `SecondaryReference`; idempotent on `commandId` |
| `DELETE /api/v1/users/me/secondary-tamagotchis/{referenceId}` | user | path `referenceId` `UUID` | `204` → empty body |

##### Statistics and progression

| Method and path | Auth | Request | Response |
| --------------- | ---- | ------- | -------- |
| `GET /api/v1/tamagotchis/{tamagotchiId}/stats` | user | path `tamagotchiId` `UUID` | `200` → `{ "stats": Stats, "statsVersion": integer, "updatedAt": Timestamp }` |
| `PUT /api/v1/tamagotchis/{tamagotchiId}/stats` | user | `stats` `Stats`, `expectedVersion` integer | `200` → `{ "stats": Stats, "statsVersion": integer }`; `409 STALE_STATS_VERSION` on a concurrent write |
| `POST /api/v1/internal/tamagotchis/{tamagotchiId}/xp` | service | `commandId` `UUID`, `amount` integer (≥ 0), `source` string | `200` → `{ "tamagotchiId": UUID, "level": integer, "xp": integer, "leveledUp": boolean }` |
| `POST /api/v1/internal/tamagotchis/{tamagotchiId}/transfer-owner` | service | `commandId` `UUID`, `newOwnerId` `UUID`, `battleId` `UUID` | `200` → `Tamagotchi`; publishes `tamagotchi.owner_transferred` |

The stat document is written wholesale rather than patched, because only the owning package knows
which keys are meaningful together. `expectedVersion` prevents two clients of the same package from
silently overwriting each other.

##### Combat types

| Method and path | Auth | Request | Response |
| --------------- | ---- | ------- | -------- |
| `GET /api/v1/combat-types` | — | — | `200` → `{ "items": [{ "type": CombatType, "strongAgainst": CombatType, "weakAgainst": CombatType }] }` |
| `GET /api/v1/combat-types/advantage` | service | query `attacker` `CombatType`, `defender` `CombatType` | `200` → `{ "multiplier": number }` |

The ring is fixed: `flame → nature → earth → electric → water → shadow → flame`. `multiplier` is
`1.5` when the attacker is strong against the defender, `0.75` when weak, and `1.0` otherwise.
Battle and Monster Raid read this instead of hard-coding the table, so the ring has one owner.

#### 3. Package Registry Service — `http://package-registry:8083`

**`Package`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `packageId` | `UUID` | |
| `slug` | string | `^[a-z0-9-]{3,48}$`, globally unique |
| `name` | string | ≤ 64 characters |
| `description` | string | ≤ 2000 characters |
| `status` | string | `draft`, `published`, `suspended` or `retired` |
| `latestVersion` | `SemVer` \| null | |
| `moderatorIds` | array of `UUID` | Users who may publish versions for this package |
| `createdAt` / `updatedAt` | `Timestamp` | |

**`PackageVersion`** — immutable once published.

| Field | Type | Notes |
| ----- | ---- | ----- |
| `packageId` | `UUID` | |
| `version` | `SemVer` | Unique within the package |
| `statDefinitions` | array of `StatDefinition` | |
| `growthRules` | object | Package-local growth mechanics, opaque to every other service |
| `publishedAt` | `Timestamp` | |

**`StatDefinition`** — the interpretation rules that make non-normalized stats usable.

| Field | Type | Notes |
| ----- | ---- | ----- |
| `key` | string | The key as it appears in a `Stats` document, e.g. `hunger` |
| `label` | string | Display name |
| `minValue` / `maxValue` | number | Inclusive range |
| `higherIsBetter` | boolean | Whether a large value is a good state |
| `combatBonus` | object \| null | `{ "threshold": number, "comparison": "gte" \| "lte", "multiplier": number }` |

**`Monster`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `monsterId` | `UUID` | |
| `name` | string | ≤ 64 characters |
| `description` | string | |
| `spriteRefs` | array of string | |
| `maxHp` | integer | ≥ 1 |
| `combatType` | `CombatType` | |
| `weaknesses` / `resistances` | array of `CombatType` | |
| `specialProperties` | object | Free-form, interpreted by Monster Raid |

**`RaidDefinition`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `raidDefinitionId` | `UUID` | |
| `monsterId` | `UUID` | |
| `durationSeconds` | integer | 60–86400 |
| `maxParticipants` | integer | 1–100 |
| `minGuildLevel` | integer | ≥ 0 |
| `rewardConfig` | object | `{ "globalCurrency": Currency, "xp": integer, "distribution": "equal" \| "damage_weighted" }` |
| `status` | string | `draft`, `scheduled`, `active`, `cancelled` or `expired` |
| `scheduledAt` | `Timestamp` \| null | |

##### Packages and versions

| Method and path | Auth | Request | Response |
| --------------- | ---- | ------- | -------- |
| `POST /api/v1/packages` | role `moderator` | `slug` string, `name` string, `description` string | `201` → `Package` |
| `GET /api/v1/packages` | user | query `status`, `limit`, `cursor` | `200` → `[Package]` |
| `GET /api/v1/packages/{packageId}` | user | path `packageId` `UUID` | `200` → `Package` |
| `PATCH /api/v1/packages/{packageId}` | role `moderator` | `name`, `description`, `status` | `200` → `Package` |
| `POST /api/v1/packages/{packageId}/versions` | role `moderator` | `version` `SemVer`, `statDefinitions` array, `growthRules` object | `201` → `PackageVersion`; publishes `registry.package_version_published` |
| `GET /api/v1/packages/{packageId}/versions` | user | query `limit`, `cursor` | `200` → `[PackageVersion]` |
| `GET /api/v1/packages/{packageId}/versions/{version}` | user | path `version` `SemVer` | `200` → `PackageVersion` |
| `GET /api/v1/internal/packages/{packageId}/versions/{version}/stat-definitions` | service | path parameters as above | `200` → `{ "items": [StatDefinition], "version": SemVer }` |

The internal stat-definitions route is what Battle calls before computing damage: it asks the
Registry *how to read* a number instead of requiring every package to share a schema. Versions are
immutable and callers request an explicit `version`, so a package cannot change a combat bonus
underneath a battle that is already running.

##### Moderators and registrations

| Method and path | Auth | Request | Response |
| --------------- | ---- | ------- | -------- |
| `POST /api/v1/packages/{packageId}/moderators` | role `admin` | `userId` `UUID` | `201` → `{ "packageId": UUID, "userId": UUID, "grantedAt": Timestamp }` |
| `DELETE /api/v1/packages/{packageId}/moderators/{userId}` | role `admin` | path parameters | `204` → empty body |
| `POST /api/v1/packages/{packageId}/registrations` | user | `userId` `UUID` | `201` → `{ "packageId": UUID, "userId": UUID, "registeredAt": Timestamp }` |
| `DELETE /api/v1/packages/{packageId}/registrations/{userId}` | user | path parameters | `204` → empty body |
| `GET /api/v1/users/{userId}/packages` | user | path `userId` `UUID`, query `limit`, `cursor` | `200` → `[Package]` |

Registrations live here and not in User Management, per
[Open Boundary Decision 1](#open-boundary-decisions): the Registry already owns package lifecycle,
so it owns the link as well and User Management reads it.

##### Monsters and raid definitions

| Method and path | Auth | Request | Response |
| --------------- | ---- | ------- | -------- |
| `POST /api/v1/monsters` | role `admin` | `name`, `description`, `spriteRefs`, `maxHp`, `combatType`, `weaknesses`, `resistances`, `specialProperties` | `201` → `Monster` |
| `GET /api/v1/monsters` | user | query `limit`, `cursor` | `200` → `[Monster]` |
| `GET /api/v1/monsters/{monsterId}` | user | path `monsterId` `UUID` | `200` → `Monster` |
| `PATCH /api/v1/monsters/{monsterId}` | role `admin` | any mutable field above | `200` → `Monster` |
| `POST /api/v1/raid-definitions` | role `admin` | `monsterId` `UUID`, `durationSeconds`, `maxParticipants`, `minGuildLevel`, `rewardConfig`, `scheduledAt` | `201` → `RaidDefinition` |
| `GET /api/v1/raid-definitions` | user | query `status`, `limit`, `cursor` | `200` → `[RaidDefinition]` |
| `GET /api/v1/internal/raid-definitions/{raidDefinitionId}` | service | path `raidDefinitionId` `UUID` | `200` → `RaidDefinition` with the embedded `Monster` |
| `POST /api/v1/raid-definitions/{raidDefinitionId}/activate` | role `admin` | — | `200` → `RaidDefinition`; publishes `registry.raid_definition_activated` |
| `POST /api/v1/raid-definitions/{raidDefinitionId}/cancel` | role `admin` | `reason` string | `200` → `RaidDefinition` with `status: "cancelled"` |

#### 4. Battle Service — `http://battle:8084`

**`Battle`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `battleId` | `UUID` | Also the idempotency key for every downstream effect |
| `status` | string | `pending`, `active`, `finished`, `declined`, `forfeited` or `expired` |
| `challenger` / `opponent` | `BattleParticipant` | `opponent.lineup` is `null` until the challenge is accepted |
| `currentTurnUserId` | `UUID` \| null | `null` unless `status` is `active` |
| `turnNumber` | integer | Starts at 1 |
| `winnerId` / `loserId` | `UUID` \| null | Set only when `status` is `finished` |
| `packageVersion` | `SemVer` | The stat-definition version pinned at acceptance |
| `createdAt` / `finishedAt` | `Timestamp` \| null | |

**`BattleParticipant`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `userId` | `UUID` | |
| `lineup` | object \| null | `{ "primaryTamagotchiId": UUID, "secondaryTamagotchiId": UUID \| null, "boosts": [UUID] }` |
| `currentHp` / `maxHp` | integer | Starting HP derives from the primary and secondary levels |

**`Turn`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `turnNumber` | integer | |
| `actorId` | `UUID` | |
| `action` | string | `attack`, `special` or `use_boost` |
| `damageDealt` | integer | ≥ 0 |
| `typeMultiplier` | number | From `GET /api/v1/combat-types/advantage` |
| `statBonusMultiplier` | number | From the pinned `StatDefinition.combatBonus` rules |
| `createdAt` | `Timestamp` | |

##### Match lifecycle

| Method and path | Auth | Request | Response |
| --------------- | ---- | ------- | -------- |
| `POST /api/v1/battles` | user | `opponentId` `UUID`, `primaryTamagotchiId` `UUID`, `secondaryTamagotchiId` `UUID` \| null, `boosts` array of `UUID` | `201` → `Battle` with `status: "pending"`; publishes `battle.created` |
| `POST /api/v1/battles/{battleId}/accept` | user | `primaryTamagotchiId` `UUID`, `secondaryTamagotchiId` `UUID` \| null, `boosts` array of `UUID` | `200` → `Battle` with `status: "active"` |
| `POST /api/v1/battles/{battleId}/decline` | user | path `battleId` `UUID` | `200` → `Battle` with `status: "declined"` |
| `POST /api/v1/battles/{battleId}/forfeit` | user | path `battleId` `UUID` | `200` → `Battle`; resolved as a loss for the caller |
| `GET /api/v1/battles/{battleId}` | user | path `battleId` `UUID` | `200` → `Battle`; `403` unless the caller is a participant |
| `GET /api/v1/battles` | user | query `userId` `UUID`, `status`, `limit`, `cursor` | `200` → `[Battle]` |
| `GET /api/v1/battles/{battleId}/turns` | user | query `limit`, `cursor` | `200` → `[Turn]` |

##### Turns

| Method and path | Auth | Request | Response |
| --------------- | ---- | ------- | -------- |
| `POST /api/v1/battles/{battleId}/turns` | user | `commandId` `UUID`, `action` string, `boostId` `UUID` \| null | `200` → `TurnResult` |

`TurnResult`:

```json
{
  "battleId": "3c9e1f70-5b2d-4a88-91cf-64bd0a2e7c15",
  "turn": {
    "turnNumber": 7,
    "actorId": "9f1c2b7e-3b2a-4c1d-9f31-2a7c5d0e4b11",
    "action": "attack",
    "damageDealt": 148,
    "typeMultiplier": 1.5,
    "statBonusMultiplier": 1.1,
    "createdAt": "2026-09-10T14:25:31.482Z"
  },
  "challengerHp": 612,
  "opponentHp": 274,
  "nextTurnUserId": "77b1c0de-9a41-4e2f-8c0b-3d5a1e9f0c22",
  "battleStatus": "active",
  "winnerId": null
}
```

A repeat of the same `commandId` returns the identical `TurnResult` with `200 OK` and applies no
further damage. Errors: `403 NOT_YOUR_TURN`, `409 BATTLE_NOT_ACTIVE`, `422 BOOST_NOT_OWNED`.

##### Synchronous dependencies

Before the first turn, Battle resolves everything it needs and pins it for the duration of the
match, so a mid-battle stat edit cannot change the arithmetic retroactively:

| Call | Target | Purpose |
| ---- | ------ | ------- |
| `GET /api/v1/internal/tamagotchis?ids=…` | Tamagotchi | Levels, combat types and current stat documents for all four creatures |
| `GET /api/v1/internal/packages/{packageId}/versions/{version}/stat-definitions` | Package Registry | How to read those stats — maxima and combat-bonus thresholds |
| `GET /api/v1/combat-types/advantage` | Tamagotchi | The type multiplier for each attacking pair |
| `GET /api/v1/internal/relationships` | User Management | Rejects a challenge between users who are neither friends nor within proximity range |

Battle writes to no other service's store. On completion it publishes one `battle.finished` event
carrying the full outcome; Tamagotchi transfers the loser's primary creature, User Management
applies both currency changes and the XP split, and Notification pushes the result — each keyed on
`battleId`.

#### 5. Map Service — `http://map:8085`

Positions are ephemeral by design: Redis holds the latest coordinate per user with a five-minute
TTL, and anything older is treated as absent rather than stale. The service reports proximity; it
never creates a battle and never sends a notification.

**`Position`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `userId` | `UUID` | |
| `latitude` / `longitude` | `Coordinate` | WGS 84 decimal degrees |
| `accuracyMeters` | number | Reported by the device; positions above 100 m are ignored |
| `recordedAt` | `Timestamp` | Device clock, rejected if more than 60 s in the future |
| `expiresAt` | `Timestamp` | `recordedAt` + 5 minutes |

**`NearbyUser`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `userId` | `UUID` | |
| `distanceMeters` | number | Rounded to 1 m; never exact for strangers |
| `relation` | string | `friend`, `enemy` or `none`, resolved through User Management |
| `latitude` / `longitude` | `Coordinate` \| null | `null` for a stranger — only bearing and distance are exposed |
| `lastSeenAt` | `Timestamp` | |

##### REST

| Method and path | Auth | Request | Response |
| --------------- | ---- | ------- | -------- |
| `POST /api/v1/locations` | user | `latitude` `Coordinate`, `longitude` `Coordinate`, `accuracyMeters` number, `recordedAt` `Timestamp` | `202` → `{ "accepted": boolean, "expiresAt": Timestamp }` |
| `GET /api/v1/map/nearby` | user | query `radiusMeters` integer (1–5000, default `500`) | `200` → `{ "items": [NearbyUser] }` |
| `GET /api/v1/map/visible` | user | — | `200` → `{ "items": [NearbyUser] }` — friends and enemies regardless of distance |
| `DELETE /api/v1/locations/me` | user | — | `204`; removes the position immediately and stops sharing |
| `GET /api/v1/internal/positions/{userId}` | service | path `userId` `UUID` | `200` → `Position`; `404 POSITION_EXPIRED` when the TTL has passed |

`POST /api/v1/locations` exists as a fallback for clients that cannot hold a socket open. The
normal path is the stream below.

##### WebSocket — `GET /api/v1/map/stream`

Upgrade carries `Authorization: Bearer <accessToken>`. The server closes with `4401` on an invalid
token and `4429` when the client exceeds one location frame per second.

Client → server:

```json
{ "type": "location.update", "latitude": 47.0245, "longitude": 28.8323,
  "accuracyMeters": 8.5, "recordedAt": "2026-09-10T14:25:31.482Z" }
```

Server → client:

| `type` | Payload | Sent when |
| ------ | ------- | --------- |
| `map.snapshot` | `{ "nearby": [NearbyUser], "generatedAt": Timestamp }` | Immediately after the socket opens |
| `map.nearby` | `{ "added": [NearbyUser], "removed": [UUID], "updated": [NearbyUser] }` | A visible user enters, leaves or moves |
| `map.proximity` | `{ "userId": UUID, "distanceMeters": number, "relation": "none", "detectedAt": Timestamp }` | Two previously unrelated users cross the 6 m threshold |
| `map.error` | `{ "code": string, "message": string }` | A frame is rejected without closing the socket |
| `map.pong` | `{ "serverTime": Timestamp }` | Reply to a `map.ping` frame; the client pings every 30 s |

Only the `map.proximity` case produces an event on the broker. The threshold is **6 metres**,
crossing is edge-triggered, and a pair is suppressed for 10 minutes after firing so that two people
standing together do not generate a stream of duplicates.

#### 6. Notification Service — `http://notification:8086`

A pure subscriber: it owns no domain state and exposes REST only for device registration,
preferences and delivery history. Everything it sends originates from an event published by another
service.

**`Device`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `deviceId` | `UUID` | |
| `userId` | `UUID` | |
| `fcmToken` | string | Firebase Cloud Messaging registration token |
| `platform` | string | `android`, `ios` or `web` |
| `locale` | string | BCP 47, e.g. `ro-MD` |
| `lastSeenAt` | `Timestamp` | Refreshed on every successful delivery |

**`Notification`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `notificationId` | `UUID` | |
| `userId` | `UUID` | |
| `category` | string | See the table below |
| `title` / `body` | string | Localized at send time |
| `data` | object | Deep-link payload, e.g. `{ "battleId": UUID }` |
| `status` | string | `queued`, `sent`, `failed` or `read` |
| `sourceEventId` | `UUID` | The envelope id that produced it; also the deduplication key |
| `createdAt` / `readAt` | `Timestamp` \| null | |

##### REST

| Method and path | Auth | Request | Response |
| --------------- | ---- | ------- | -------- |
| `POST /api/v1/devices` | user | `fcmToken` string, `platform` string, `locale` string | `201` → `Device`; re-registering the same token returns `200` |
| `GET /api/v1/devices` | user | — | `200` → `{ "items": [Device] }` |
| `DELETE /api/v1/devices/{deviceId}` | user | path `deviceId` `UUID` | `204` → empty body |
| `GET /api/v1/notifications` | user | query `status`, `category`, `limit`, `cursor` | `200` → `[Notification]` |
| `POST /api/v1/notifications/{notificationId}/read` | user | path `notificationId` `UUID` | `200` → `Notification` with `status: "read"` |
| `POST /api/v1/notifications/read-all` | user | — | `200` → `{ "updated": integer }` |
| `GET /api/v1/notifications/preferences` | user | — | `200` → `{ "items": [{ "category": string, "push": boolean, "quietHours": { "from": "22:00", "to": "08:00" } \| null }] }` |
| `PUT /api/v1/notifications/preferences` | user | `items` array as above | `200` → the stored preferences |

##### Delivery matrix

| Category | Triggering event | Deep-link `data` |
| -------- | ---------------- | ---------------- |
| `friend_request` | `user.friend_request_created` | `{ "requestId": UUID, "fromUserId": UUID }` |
| `nearby_player` | `map.players_nearby` | `{ "userId": UUID, "distanceMeters": number }` |
| `battle_request` | `battle.created` | `{ "battleId": UUID, "challengerId": UUID }` |
| `battle_result` | `battle.finished` | `{ "battleId": UUID, "winnerId": UUID }` |
| `tamagotchi_captured` | `tamagotchi.owner_transferred` | `{ "tamagotchiId": UUID, "newOwnerId": UUID }` |
| `guild_invitation` | `guild.invitation_created` | `{ "guildId": UUID, "invitationId": UUID }` |
| `raid_started` | `raid.started` | `{ "raidId": UUID, "guildId": UUID }` |
| `raid_finished` | `raid.monster_defeated`, `raid.expired` | `{ "raidId": UUID, "outcome": string }` |

Delivery is best-effort. A Firebase failure is retried with exponential backoff up to five times,
after which the notification is marked `failed` and the message is dead-lettered — a push that
cannot be delivered never blocks the gameplay service that produced the event. A token rejected by
Firebase as unregistered deletes the corresponding `Device` row.

#### 7. Guild Service — `http://guild:8087`

**`Guild`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `guildId` | `UUID` | |
| `name` | string | 3–48 characters, unique |
| `tag` | string | 2–5 uppercase characters, unique, shown next to member names |
| `description` | string | ≤ 512 characters |
| `emblemRef` | string \| null | |
| `ownerId` | `UUID` | Exactly one owner at all times |
| `memberCount` | integer | Denormalized counter, maintained by this service |
| `maxMembers` | integer | Default `50` |
| `level` | integer | ≥ 1; gates which raid definitions the guild may start |
| `createdAt` | `Timestamp` | |

**`Membership`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `guildId` / `userId` | `UUID` | Composite key |
| `role` | string | `owner`, `officer` or `member` |
| `joinedAt` | `Timestamp` | |

**`Invitation`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `invitationId` | `UUID` | |
| `guildId` | `UUID` | |
| `invitedUserId` / `invitedByUserId` | `UUID` | |
| `status` | string | `pending`, `accepted`, `declined` or `expired` |
| `expiresAt` | `Timestamp` | 7 days after creation |

**`ChatMessage`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `messageId` | `UUID` | Server-assigned |
| `clientMessageId` | `UUID` | Echoed back so the sender can reconcile its optimistic copy |
| `guildId` / `authorId` | `UUID` | |
| `body` | string | 1–1000 characters after trimming |
| `sentAt` | `Timestamp` | Server clock, authoritative for ordering |

##### Guilds and membership

| Method and path | Auth | Request | Response |
| --------------- | ---- | ------- | -------- |
| `POST /api/v1/guilds` | user | `name` string, `tag` string, `description` string, `emblemRef` string \| null | `201` → `Guild`; the creator becomes `owner` |
| `GET /api/v1/guilds` | user | query `query` string, `limit`, `cursor` | `200` → `[Guild]` |
| `GET /api/v1/guilds/{guildId}` | user | path `guildId` `UUID` | `200` → `Guild` |
| `PATCH /api/v1/guilds/{guildId}` | role `owner` \| `officer` | `description`, `emblemRef` | `200` → `Guild` |
| `DELETE /api/v1/guilds/{guildId}` | role `owner` | — | `204`; refused with `409 RAID_IN_PROGRESS` while a raid is active |
| `GET /api/v1/guilds/{guildId}/members` | user | query `role`, `limit`, `cursor` | `200` → `[Membership]` |
| `PATCH /api/v1/guilds/{guildId}/members/{userId}` | role `owner` | `role` string | `200` → `Membership`; transferring `owner` demotes the previous owner to `officer` |
| `DELETE /api/v1/guilds/{guildId}/members/{userId}` | role `owner` \| `officer` \| self | path parameters | `204`; publishes `guild.member_left` |
| `GET /api/v1/internal/guilds/{guildId}/members/{userId}` | service | path parameters | `200` → `{ "isMember": boolean, "role": string \| null, "joinedAt": Timestamp \| null }` |

The internal membership check is the call Monster Raid makes before admitting a participant — a
raid cannot admit a player without confirming membership, so this one is synchronous rather than
event-driven.

##### Invitations

| Method and path | Auth | Request | Response |
| --------------- | ---- | ------- | -------- |
| `POST /api/v1/guilds/{guildId}/invitations` | role `owner` \| `officer` | `userId` `UUID` | `201` → `Invitation`; publishes `guild.invitation_created` |
| `GET /api/v1/guilds/{guildId}/invitations` | role `owner` \| `officer` | query `status`, `limit`, `cursor` | `200` → `[Invitation]` |
| `GET /api/v1/users/me/invitations` | user | query `status`, `limit`, `cursor` | `200` → `[Invitation]` |
| `POST /api/v1/invitations/{invitationId}/accept` | user | path `invitationId` `UUID` | `200` → `Membership`; publishes `guild.member_joined` |
| `POST /api/v1/invitations/{invitationId}/decline` | user | path `invitationId` `UUID` | `200` → `Invitation` with `status: "declined"` |

Before creating an invitation the service calls `GET /api/v1/users/{userId}` on User Management to
confirm the invitee exists; a missing user is rejected with `404 USER_NOT_FOUND` rather than stored
as a dangling reference.

##### Chat history and WebSocket — `GET /api/v1/guilds/{guildId}/chat`

| Method and path | Auth | Request | Response |
| --------------- | ---- | ------- | -------- |
| `GET /api/v1/guilds/{guildId}/messages` | member | query `limit`, `cursor` | `200` → `[ChatMessage]`, newest first |

The socket upgrade requires a member JWT; a non-member is closed with `4403`. Because several
FastAPI instances can serve the same guild, a message is written to PostgreSQL and republished on
the Redis channel `guild:{guildId}:chat`, from which every instance fans it out to its own sockets.

Client → server:

| `type` | Payload |
| ------ | ------- |
| `chat.send` | `{ "clientMessageId": UUID, "body": string }` |
| `chat.typing` | `{ "isTyping": boolean }` — not persisted |
| `chat.ping` | `{}` — heartbeat, every 30 s |

Server → client:

| `type` | Payload | Sent when |
| ------ | ------- | --------- |
| `chat.history` | `{ "items": [ChatMessage] }` | Immediately after the socket opens — the last 50 messages |
| `chat.message` | `ChatMessage` | Any member sends a message |
| `chat.typing` | `{ "userId": UUID, "isTyping": boolean }` | Another member starts or stops typing |
| `chat.presence` | `{ "userId": UUID, "state": "online" \| "offline" }` | A member connects or disconnects |
| `chat.error` | `{ "code": string, "message": string, "clientMessageId": UUID \| null }` | A frame is rejected — `MESSAGE_TOO_LONG`, `RATE_LIMITED` |
| `chat.pong` | `{ "serverTime": Timestamp }` | Reply to `chat.ping` |

Rate limit: 5 messages per 10 seconds per member. Exceeding it yields `chat.error` with
`RATE_LIMITED` rather than a disconnect.

#### 8. Monster Raid Service — `http://monster-raid:8088`

**`Raid`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `raidId` | `UUID` | Idempotency key for reward distribution |
| `guildId` | `UUID` | |
| `raidDefinitionId` / `monsterId` | `UUID` | Copied from the Registry definition at creation |
| `monsterName` | string | Denormalized for display only |
| `maxHp` / `currentHp` | integer | `currentHp` never drops below 0 |
| `status` | string | `active`, `defeated`, `expired` or `cancelled` |
| `participantCount` | integer | |
| `startedAt` / `expiresAt` / `endedAt` | `Timestamp` \| null | `expiresAt` = `startedAt` + `durationSeconds` |

**`RaidParticipant`**

| Field | Type | Notes |
| ----- | ---- | ----- |
| `userId` | `UUID` | |
| `tamagotchiId` | `UUID` | The member's primary creature, resolved at join time |
| `combatType` | `CombatType` | Pinned at join; decides the weakness multiplier |
| `damageDealt` | integer | Running total |
| `attackCount` | integer | |
| `joinedAt` | `Timestamp` | |

##### Raid lifecycle

| Method and path | Auth | Request | Response |
| --------------- | ---- | ------- | -------- |
| `POST /api/v1/raids` | role `owner` \| `officer` | `guildId` `UUID`, `raidDefinitionId` `UUID` | `201` → `Raid`; publishes `raid.started` |
| `GET /api/v1/raids/{raidId}` | member | path `raidId` `UUID` | `200` → `Raid` |
| `GET /api/v1/guilds/{guildId}/raids` | member | query `status`, `limit`, `cursor` | `200` → `[Raid]` |
| `POST /api/v1/raids/{raidId}/participants` | member | `primaryTamagotchiId` `UUID` | `201` → `RaidParticipant` |
| `GET /api/v1/raids/{raidId}/participants` | member | query `limit`, `cursor` | `200` → `[RaidParticipant]` |
| `DELETE /api/v1/raids/{raidId}/participants/me` | member | — | `204`; damage already dealt is retained |
| `GET /api/v1/raids/{raidId}/leaderboard` | member | query `limit` integer (default `20`) | `200` → `{ "items": [{ "rank": integer, "userId": UUID, "damageDealt": integer, "share": number }] }` |
| `POST /api/v1/raids/{raidId}/cancel` | role `admin` | `reason` string | `200` → `Raid` with `status: "cancelled"` |

Joining is refused with `403 NOT_A_GUILD_MEMBER` (checked against Guild), `409 RAID_FULL` when
`participantCount` has reached `maxParticipants`, and `409 RAID_NOT_ACTIVE` once the raid has ended.

##### Attacks

| Method and path | Auth | Request | Response |
| --------------- | ---- | ------- | -------- |
| `POST /api/v1/raids/{raidId}/attacks` | participant | `commandId` `UUID` | `200` → `AttackResult` |

`AttackResult`:

```json
{
  "raidId": "b81f0a63-77de-4f2c-9a10-5c2e7d3b8410",
  "commandId": "0e2a5c19-4d7b-4f0e-8a3c-11b9f6d2e7a4",
  "damageDealt": 214,
  "typeMultiplier": 1.5,
  "monsterHpRemaining": 48320,
  "yourTotalDamage": 12844,
  "raidStatus": "active",
  "attackedAt": "2026-09-10T14:25:31.482Z"
}
```

The HP decrement is a single atomic Redis `DECRBY` clamped at zero, because an entire guild hits one
counter concurrently. The result is appended to `damage_log` in PostgreSQL for durability, and the
transition to `defeated` is applied exactly once with a conditional update — the first attack that
drives HP to zero wins the transition and publishes the terminal event; every later attack receives
`409 RAID_NOT_ACTIVE`. Rate limit: 10 attacks per second per participant.

##### WebSocket — `GET /api/v1/raids/{raidId}/feed`

Server-push only; the client sends nothing but heartbeats. A non-participant is closed with `4403`.

| `type` | Payload | Sent when |
| ------ | ------- | --------- |
| `raid.snapshot` | `{ "raid": Raid, "participants": [RaidParticipant] }` | Immediately after the socket opens |
| `raid.damage` | `{ "userId": UUID, "damageDealt": integer, "monsterHpRemaining": integer }` | Any participant lands an attack, throttled to 10 frames per second |
| `raid.participant_joined` | `RaidParticipant` | A member joins |
| `raid.ended` | `{ "status": "defeated" \| "expired", "endedAt": Timestamp, "rewards": [{ "userId": UUID, "globalCurrency": Currency, "xp": integer }] \| null }` | The monster dies or the timer expires |
| `raid.pong` | `{ "serverTime": Timestamp }` | Reply to `raid.ping` |

Rewards appear in `raid.ended` for immediate display, but the service does not credit them. It
publishes `raid.monster_defeated`; User Management applies the currency and Tamagotchi applies the
XP, both keyed on `raidId`.

### Asynchronous event contract

All events go through a single durable topic exchange, `tamagotchi.events`. Routing keys are
`<domain>.<fact>`, always past tense — an event states something that already happened and is never
a request for someone to act.

#### Envelope

Every message has the same envelope; only `payload` differs.

```json
{
  "eventId": "7c1e0b44-92a5-4f6d-8b31-5d0a2f9e13c7",
  "eventType": "battle.finished",
  "eventVersion": 1,
  "occurredAt": "2026-09-10T14:25:31.482Z",
  "producer": "battle-service",
  "correlationId": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
  "idempotencyKey": "3c9e1f70-5b2d-4a88-91cf-64bd0a2e7c15",
  "payload": { }
}
```

| Field | Type | Notes |
| ----- | ---- | ----- |
| `eventId` | `UUID` | Unique per publication; the deduplication key for every consumer |
| `eventType` | string | Equal to the routing key |
| `eventVersion` | integer | Incremented only on an incompatible payload change |
| `occurredAt` | `Timestamp` | When the fact happened, not when it was published |
| `producer` | string | Publishing service name |
| `correlationId` | `UUID` | Copied from the request that caused the fact |
| `idempotencyKey` | `UUID` | The domain key — `battleId`, `raidId` — that makes the *effect* repeatable |

`eventId` and `idempotencyKey` are different on purpose: a republished message keeps the domain key
but gets a new `eventId`, so consumers deduplicate on `eventId` and reconcile business effects on
`idempotencyKey`.

#### Catalogue

| Routing key | Producer | Payload |
| ----------- | -------- | ------- |
| `user.registered` | User Management | `userId`, `username`, `packageId`, `registeredAt` |
| `user.friend_request_created` | User Management | `requestId`, `fromUserId`, `toUserId` |
| `user.friend_request_accepted` | User Management | `requestId`, `fromUserId`, `toUserId` |
| `user.deleted` | User Management | `userId`, `purgeAt` |
| `tamagotchi.created` | Tamagotchi | `tamagotchiId`, `ownerId`, `originPackageId`, `combatType` |
| `tamagotchi.owner_transferred` | Tamagotchi | `tamagotchiId`, `previousOwnerId`, `newOwnerId`, `battleId` |
| `tamagotchi.leveled_up` | Tamagotchi | `tamagotchiId`, `ownerId`, `level` |
| `registry.package_version_published` | Package Registry | `packageId`, `version`, `publishedAt` |
| `registry.raid_definition_activated` | Package Registry | `raidDefinitionId`, `monsterId`, `scheduledAt` |
| `battle.created` | Battle | `battleId`, `challengerId`, `opponentId` |
| `battle.finished` | Battle | see below |
| `map.players_nearby` | Map | `userA`, `userB`, `distanceMeters`, `detectedAt` |
| `guild.invitation_created` | Guild | `invitationId`, `guildId`, `invitedUserId`, `invitedByUserId` |
| `guild.member_joined` | Guild | `guildId`, `userId`, `role` |
| `guild.member_left` | Guild | `guildId`, `userId`, `reason` |
| `raid.started` | Monster Raid | `raidId`, `guildId`, `monsterId`, `expiresAt` |
| `raid.monster_defeated` | Monster Raid | see below |
| `raid.expired` | Monster Raid | `raidId`, `guildId`, `remainingHp`, `expiredAt` |

#### `battle.finished`

The most consequential event in the system — three services react to it, and each one writes state
that Battle deliberately does not own.

```json
{
  "battleId": "3c9e1f70-5b2d-4a88-91cf-64bd0a2e7c15",
  "winnerId": "9f1c2b7e-3b2a-4c1d-9f31-2a7c5d0e4b11",
  "loserId": "77b1c0de-9a41-4e2f-8c0b-3d5a1e9f0c22",
  "outcome": "knockout",
  "turnCount": 14,
  "transferredTamagotchiId": "4f2c8e1a-6b09-4d3e-a7c5-8e0b1d2f3a44",
  "currency": { "winnerDelta": 250, "loserDelta": -100 },
  "xp": {
    "winner": { "primaryTamagotchiId": "4a1b…", "primaryXp": 180, "secondaryTamagotchiId": "9c2d…", "secondaryXp": 120 },
    "loser":  { "primaryTamagotchiId": "4f2c…", "primaryXp": 60,  "secondaryTamagotchiId": null,   "secondaryXp": 0 }
  },
  "finishedAt": "2026-09-10T14:25:31.482Z"
}
```

`outcome` is `knockout`, `forfeit` or `timeout`. `transferredTamagotchiId` is the loser's primary
creature and is `null` when the outcome is `timeout`. The XP split follows the fixed 60/40 rule
between primary and secondary.

| Consumer | Effect | Keyed on |
| -------- | ------ | -------- |
| Tamagotchi | Reassigns `ownerId` of `transferredTamagotchiId`, creates a `SecondaryReference` for the winner, applies both XP grants | `battleId` |
| User Management | Applies `winnerDelta` and `loserDelta` to `globalCurrency` | `battleId` |
| Notification | Pushes `battle_result` to both participants | `eventId` |

#### `raid.monster_defeated`

```json
{
  "raidId": "b81f0a63-77de-4f2c-9a10-5c2e7d3b8410",
  "guildId": "2d7c4e91-0a3b-4f8c-91de-6b2a0c5f7e33",
  "monsterId": "e5a91c37-8b02-4d6f-a1c9-70f3b8d2e514",
  "totalDamage": 128400,
  "durationSeconds": 1730,
  "rewards": [
    { "userId": "9f1c…", "tamagotchiId": "4a1b…", "globalCurrency": 500, "xp": 300, "damageShare": 0.31 }
  ],
  "defeatedAt": "2026-09-10T14:25:31.482Z"
}
```

`rewards` is computed by Monster Raid but applied by the owners of the affected state: User
Management credits `globalCurrency`, Tamagotchi credits `xp`. Both deduplicate on `raidId`, which
is what guarantees a raid pays out exactly once even if the message is redelivered.

#### Queues and delivery

Each consumer owns a named durable queue bound to its own routing keys — no queue is shared between
two services, so a slow consumer cannot starve another.

| Queue | Bound routing keys |
| ----- | ------------------ |
| `tamagotchi.battle-outcomes` | `battle.finished` |
| `tamagotchi.raid-rewards` | `raid.monster_defeated` |
| `usermgmt.rewards` | `battle.finished`, `raid.monster_defeated` |
| `usermgmt.package-registrations` | `registry.package_version_published` |
| `notification.fanout` | `user.*`, `battle.*`, `tamagotchi.owner_transferred`, `map.players_nearby`, `guild.*`, `raid.*` |
| `guild.user-lifecycle` | `user.deleted` |
| `raid.definitions` | `registry.raid_definition_activated` |

Delivery rules, uniform across every consumer:

- messages are persistent and queues are durable, so a broker restart loses nothing;
- a consumer acknowledges **only after** its local transaction — effect plus `processed_events` row
  — has committed;
- a transient failure is negatively acknowledged and retried with exponential backoff, 5 attempts;
- after the final attempt the message is routed to `tamagotchi.events.dlq` with the original
  routing key and failure reason in the headers, and an operator replays it once the cause is fixed;
- a consumer that receives an `eventVersion` higher than it understands dead-letters the message
  instead of guessing, which is what makes the version field useful rather than decorative.

---

## Open Boundary Decisions

Points where two services could plausibly claim the same data. Resolve these before writing the
communication contract.

1. **User ↔ package registration.** Both User Management ("the packages the user is registered
   with") and Package Registry ("which users are registered with which packages") describe this
   link. Pick one writer — Registry is the better home, since it already owns package lifecycle —
   and let User Management read it.
2. **Ownership transfer after battle.** Battle decides the outcome, Tamagotchi writes the new owner.
   Battle must never write to Tamagotchi's database directly.
3. **Local currency.** Its value is package-defined but balances are per-user. Registry defines the
   rules; User Management holds the balance.
4. **Proximity → battle.** Map only reports proximity. Turning that into a battle request is the
   client's or Battle's decision, never Map's.

---

## Contribution Workflow

### Branching strategy

The repository uses a lightweight GitFlow model with two long-lived branches:

- **`main`** contains only stable, reviewed release history. Direct commits are forbidden.
- **`dev`** is the integration branch for the next release. Completed work reaches it only through
  a pull request.

All other branches are short-lived and use lowercase kebab-case names. When an issue exists, its
number is included so the branch can be traced back to the task.

| Branch pattern | Created from | Pull request target | Purpose |
| -------------- | ------------ | ------------------- | ------- |
| `feature/<issue>-<description>` | `dev` | `dev` | New functionality |
| `fix/<issue>-<description>` | `dev` | `dev` | Non-urgent bug fix |
| `docs/<issue>-<description>` | `dev` | `dev` | Documentation only |
| `refactor/<issue>-<description>` | `dev` | `dev` | Internal change without new behaviour |
| `test/<issue>-<description>` | `dev` | `dev` | Test-only changes |
| `release/v<major>.<minor>.<patch>` | `dev` | `main` | Release stabilization and metadata |
| `hotfix/<issue>-<description>` | `main` | `main` | Urgent production fix |

Examples: `feature/24-user-registration`, `fix/31-duplicate-raid-reward`,
`docs/42-communication-contract` and `release/v1.0.0`.

Regular work is branched from the latest `dev`. Release branches start from `dev`, while urgent
hotfix branches start from `main`. Force pushes and direct commits to `main` or `dev` are not
allowed.

### Pull request template

Every pull request must be small enough to review. Its description must follow this template:

```markdown
## Summary

<!-- What changed and why is this change needed? -->

## Related issue

Closes #<issue-number>

## Affected services

<!-- List the affected microservices, APIs, events and data stores. -->

- Service(s):
- API/event contracts:
- Data stores/migrations:

## Testing

<!-- List automated tests and concise manual verification steps. -->

1.

## Breaking changes

<!-- Describe compatibility impact, migration and rollout steps. Write "None" when not applicable. -->

None

## Checklist

- [ ] The branch follows the naming convention and is up to date with its target.
- [ ] The change is focused and contains no unrelated modifications.
- [ ] API, event and database changes are backward compatible or documented above.
- [ ] Documentation and communication contracts are updated where necessary.
- [ ] Automated tests pass locally and new behaviour is covered by tests.
- [ ] No credentials, secrets or personal data are committed.
```

Draft pull requests may be opened for early feedback, but they cannot be merged. A pull request is
ready for review only when every section is completed and every applicable checklist item is
checked.

### Review and approval policy

Both long-lived branches are protected and accept changes only through pull requests:

- a pull request into **`dev` requires at least one approval**;
- a pull request into **`main` requires at least two approvals**;
- the author cannot approve their own pull request;
- the latest push must be approved by another contributor;
- approvals are dismissed when new commits change the reviewed diff;
- every review conversation must be resolved before merging.

Approvals count only from collaborators with write access. Reviewers check correctness, service
boundaries, API and event compatibility, tests, security implications and documentation. Approval
means the change is ready to merge, not merely that it has been read.

### Merge strategy

The repository uses **Squash and merge** so each pull request becomes one meaningful commit and the
history of the long-lived branches stays linear. The squashed commit title must follow the
Conventional Commits format.

- Feature, fix, documentation, refactoring and test branches merge into `dev`.
- A release branch is cut only when `dev` is ready and merges into `main` after release validation.
- A hotfix merges into `main`, then `main` is synchronized back into `dev` so the fix remains in the
  next release.
- The release commit on `main` receives a version tag.
- The source branch is deleted after a successful merge.

Merge commits and rebase merging are disabled in the repository settings. A branch must be updated
with its target before merging, and failed required checks always block the merge.

### Testing and coverage

Every service must maintain automated tests appropriate to its responsibilities:

- **unit tests** cover domain rules such as damage, permissions, rewards and stat validation;
- **integration tests** cover the service's own PostgreSQL/Redis access and RabbitMQ consumers and
  publishers;
- **contract tests** verify REST/OpenAPI and event schemas across the Go and Python boundary;
- **regression tests** reproduce every confirmed bug before its fix is merged.

Each service must maintain at least **80% line coverage**. Generated code, migrations and trivial
bootstrap files may be excluded, but lowering the threshold requires an explanation in the pull
request and reviewer approval. Coverage is a guardrail rather than a substitute for meaningful
assertions, so critical authentication, ownership-transfer and reward paths require explicit tests.

CI runs formatting/linting, tests and coverage checks for every pull request. A pull request with a
failed check, coverage below the threshold or missing tests for changed behaviour cannot be merged.

### Versioning and commit convention

Each microservice is versioned independently using **Semantic Versioning** in the form
`MAJOR.MINOR.PATCH`:

- **MAJOR** changes an API or event contract incompatibly;
- **MINOR** adds backward-compatible functionality;
- **PATCH** contains backward-compatible fixes.

Stable releases are tagged `v<major>.<minor>.<patch>` in the corresponding service repository, for
example `v1.4.2`. Release candidates may use a suffix such as `v2.0.0-rc.1`. Published tags are
immutable; a correction requires a new version. REST paths and event envelopes carry an explicit
major contract version when incompatible versions must coexist.

Commit and squash-merge titles follow **Conventional Commits**:

```text
<type>(optional-scope): <short imperative description>
```

Use the commit type that best describes the primary purpose of the change:

| Type | Use for | Example |
| ---- | ------- | ------- |
| `feat` | A new user-visible or API capability | `feat(battle): add secondary creature selection` |
| `fix` | A bug fix that restores expected behaviour | `fix(raid): prevent duplicate rewards` |
| `docs` | Documentation-only changes | `docs: define branching strategy` |
| `test` | Adding or correcting tests without changing production behaviour | `test(user): cover expired JWT` |
| `refactor` | Internal restructuring without a feature or bug fix | `refactor(map): extract proximity calculator` |
| `perf` | A measurable performance improvement | `perf(raid): batch damage updates` |
| `build` | Build system, packaging or dependency changes | `build: update Go toolchain` |
| `ci` | CI/CD workflows and automation | `ci: add coverage check` |
| `chore` | Maintenance not covered by another type | `chore: update repository settings` |
| `revert` | Reverting an earlier commit | `revert: feat(battle): add rematch` |

The optional scope identifies the affected service or area, such as `battle`, `raid`, `user` or
`docs`. An incompatible change adds `!` after the type/scope or a `BREAKING CHANGE:` footer, for
example `fix(raid)!: reject attacks using the legacy payload`.
