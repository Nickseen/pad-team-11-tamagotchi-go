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

Regular work is branched from the latest `dev` and merged back into `dev`. A release branch is cut
only when the integration branch is ready; after it is merged into `main`, the release is tagged and
`main` is synchronized back into `dev`. A hotfix follows the same synchronization rule so the fix is
not lost in the next release. Merged branches are deleted. Force pushes and direct commits to
`main` or `dev` are not allowed.

### Pull request content

Every pull request must be small enough to review and contain the following information in its
description:

- **Summary:** what changed and why the change is needed.
- **Related issue:** `Closes #<issue-number>` or an explanation when no issue exists.
- **Affected services:** the microservices, APIs, events and data stores touched by the change.
- **Contract and data changes:** new or changed endpoints, event schemas, database migrations and
  backward-compatibility considerations.
- **Testing:** tests added or updated and concise steps a reviewer can use to verify the result.
- **Breaking changes:** migration or rollout instructions; write `None` when there are none.

Before requesting review, the author confirms that:

- [ ] the branch follows the naming convention and is up to date with its target branch;
- [ ] the change is focused and contains no unrelated modifications;
- [ ] documentation and communication contracts are updated where necessary;
- [ ] automated tests pass locally and new behaviour is covered by tests;
- [ ] no credentials, secrets or personal data are committed.

Draft pull requests may be opened for early feedback, but they cannot be merged. A pull request is
ready for review only when its description and checklist are complete.
