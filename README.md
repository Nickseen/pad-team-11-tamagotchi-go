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
{ "items": [ /* resource objects */ ], "nextCursor": "b3RoZXI6MTIz", "hasMore": true }
```

`nextCursor` is `null` when `hasMore` is `false`.

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
