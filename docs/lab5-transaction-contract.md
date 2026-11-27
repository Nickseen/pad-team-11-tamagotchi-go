# Lab 5: distributed transaction contract (two-phase commit)

Status: merged with CPR #206 for #196. The participants' owners (@vladas9, @CyberMoonie, @Nickseen) and the
scenario owner (@gabimiric) raise problems on #196 before their implementation merges. #197 replaces the
two phases with Saga steps and keeps everything marked *kept by Saga* below.

Two business operations change several databases at once. Until Lab 4 each consumer applied its part of
`battle.finished` or `raid.monster_defeated` on its own. Here they commit together or not at all.

| Operation | Business key | Initiator | Participants, each writing only its own PostgreSQL |
| --- | --- | --- | --- |
| `battle.settlement` | `battleId` | Battle | User Management, Tamagotchi, Battle |
| `raid.payout` | `raidId` | Monster Raid | User Management, Tamagotchi, Monster Raid |

The **coordinator** is the Message Broker. It keeps every transaction in its journal (`DATA_DIR`) and
sends the phases itself, over HTTP, to one ready replica of each participant, like event deliveries. A
different replica may receive each phase: the state lives in the participant's database.

```mermaid
sequenceDiagram
    participant B as Battle (initiator)
    participant C as Broker (coordinator)
    participant U as User Management
    participant T as Tamagotchi
    B->>B: match decided + start request in the outbox (one local transaction)
    B->>C: POST /api/v1/transactions
    C->>C: journal the transaction
    C-->>B: 202
    par prepare
        C->>U: prepare
        C->>T: prepare
        C->>B: prepare
    end
    Note over U,B: PREPARE TRANSACTION, locks held, vote
    C->>C: journal the decision (all voted: commit, any reject: rollback)
    par commit
        C->>U: commit
        C->>T: commit
    end
    C->>B: commit (initiator last)
    B->>B: settled; battle.finished leaves the outbox
```

## Identifiers (kept by Saga)

| Field | Meaning |
| --- | --- |
| `transactionId` | UUID created by the initiator with the start request and stored with it, so a resent request repeats it |
| `operation`, `operationVersion` | One of the two operations above; version `1` |
| `businessKey` | `battleId` or `raidId`. At most one transaction exists per operation and business key |
| `attempt` | Starts at 1. Every new voting round of one transaction raises it by one |
| `payload` | Exactly the `payload` of `battle.finished` v1 or `raid.monster_defeated` v1 ([README](../README.md#asynchronous-event-contract)), so participants read the fields they already read |
| branch | One participant's part of one attempt. Its PostgreSQL identifier is `tg:<transactionId>:<attempt>` |

## Broker API (kept by Saga)

All routes need `X-Service-Token`; errors use the shared envelope. They are internal: the Gateway does not
expose them, and `127.0.0.1:8091` is for inspection.

`POST /api/v1/transactions`

```json
{
  "transactionId": "5d0c1f0e-7b1e-4c86-9a53-0f2b6f6c1a11",
  "operation": "battle.settlement",
  "operationVersion": 1,
  "businessKey": "3c9e1f70-5b2d-4a88-91cf-64bd0a2e7c15",
  "correlationId": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
  "payload": { "battleId": "3c9e1f70-5b2d-4a88-91cf-64bd0a2e7c15", "winnerId": "…" }
}
```

| Result | When |
| --- | --- |
| `202` with the transaction | Stored on disk; the coordinator starts it |
| `200` with the transaction | The same `transactionId`, or the same operation and business key, is already known. Nothing starts twice |
| `400 VALIDATION_FAILED` | Unknown operation or version, a malformed field, or `payload.battleId`/`payload.raidId` differs from `businessKey` |
| `409 TRANSACTION_CONFLICT` | A known `transactionId` with another operation or business key |
| `503 DEPENDENCY_UNAVAILABLE` | The journal cannot be written; send it again |

`GET /api/v1/transactions/{transactionId}` returns one transaction (`404 TRANSACTION_NOT_FOUND`), and
`GET /api/v1/transactions?operation=&businessKey=&status=&offset=0&limit=50` a page, newest first.

```json
{
  "transactionId": "5d0c1f0e-…", "operation": "battle.settlement", "businessKey": "3c9e1f70-…",
  "status": "committed", "attempt": 1, "decision": "commit", "reason": null,
  "participants": [
    { "service": "user-management", "state": "committed", "lastError": null },
    { "service": "tamagotchi", "state": "committed", "lastError": null },
    { "service": "battle", "state": "committed", "lastError": null }
  ],
  "createdAt": "2026-11-25T10:00:00.000Z", "updatedAt": "2026-11-25T10:00:00.120Z"
}
```

| `status` | Meaning |
| --- | --- |
| `preparing` | Waiting for votes |
| `committing` | Commit is decided and on disk; participants are being told |
| `rolling_back` | Rollback of this attempt is decided; participants are being told |
| `waiting` | No decision was possible; the next attempt starts after a backoff |
| `committed`, `rolled_back` | Final |

## Participant API

Each participant serves four service routes, authenticated by `X-Service-Token` like
`/api/v1/internal/events` and counted against `MAX_CONCURRENT_TASKS`:

| Route | Purpose |
| --- | --- |
| `POST /api/v1/internal/transactions/{transactionId}/prepare` | Vote |
| `POST /api/v1/internal/transactions/{transactionId}/commit` | Make the branch permanent |
| `POST /api/v1/internal/transactions/{transactionId}/rollback` | Discard the branch |
| `GET /api/v1/internal/transactions/prepared` | The branches still in doubt: `{ "items": [{ "transactionId", "attempt", "preparedAt" }] }` |

The three POSTs carry the same body, so every phase can be repeated on its own:

```json
{
  "transactionId": "5d0c1f0e-…", "attempt": 1,
  "operation": "battle.settlement", "operationVersion": 1, "businessKey": "3c9e1f70-…",
  "payload": { },
  "final": true, "reason": "TAMAGOTCHI_NOT_OWNED"
}
```

`final` and `reason` are sent with `rollback` only. `final: true` ends the transaction; `false` means
another attempt follows. A rollback from the recovery scan may belong to a transaction the coordinator does
not know: then `operation`, `operationVersion`, `businessKey` and `payload` are `null` and `reason` is
`UNKNOWN_TRANSACTION`. Headers: `X-Service-Token`, `X-Correlation-Id` (the start request's) and
`X-Broker-Attempt` (the number of this send).

| Phase | `200` answer | Meaning |
| --- | --- | --- |
| prepare | `{ "vote": "commit" }` | The branch is prepared, now or by an earlier send of the same attempt |
| prepare | `{ "vote": "already_applied" }` | The business key already took effect here; nothing is prepared. Counts as a yes |
| prepare | `{ "vote": "reject", "code": "…", "message": "…" }` | A business rule forbids it; nothing is prepared. Ends the transaction |
| commit | `{ "status": "committed" }` | Also for a repeated commit |
| rollback | `{ "status": "rolled_back" }` | Also when there was nothing to roll back |

**Anything else is no answer, never a vote**: the coordinator sends the phase again. So an unreachable
replica, an old release without the route (`404`), a full task limit (`429`) or a wrong token (`401`)
delays a settlement and never loses it. A branch that waits longer than its lock timeout answers
`429 TRANSACTION_BUSY` with `Retry-After: 1`. Phases bypass the circuit breakers and are not reported to
Service Discovery: they are idempotent and repeated without end, so counting them would remove every replica
of a service whose database is failing over. Each send goes to another replica than the one that just failed.

## What a participant does in PostgreSQL

One table, added by the owner's own migration:

```sql
CREATE TABLE transaction_branches (
    transaction_id UUID        NOT NULL,
    attempt        INTEGER     NOT NULL,
    outcome        TEXT        NOT NULL CHECK (outcome IN ('committed', 'rolled_back')),
    operation      TEXT,  -- null only for the rollback of a transaction the coordinator does not know
    business_key   UUID,  -- likewise
    effect         JSONB       NOT NULL DEFAULT '{}',  -- what was really applied, see the Saga contract
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (transaction_id, attempt)
);
```

### Prepare

1. If `pg_prepared_xacts` has the branch, answer `commit`.
2. In one transaction:

   ```sql
   BEGIN;
   SET LOCAL lock_timeout = '2s';
   SET LOCAL synchronous_commit = on;
   INSERT INTO transaction_branches (…, outcome, …) VALUES (…, 'committed', …);
   -- the legacy event's own deduplication key, so event and transaction exclude each other:
   -- event_id is the transactionId, the event type and key are the event's
   INSERT INTO processed_events (…) VALUES (:transactionId, 'battle.finished', :battleId) ON CONFLICT DO NOTHING;
   -- check the rules and apply the effect; lock rows in ascending ID order
   PREPARE TRANSACTION 'tg:<transactionId>:<attempt>';
   ```

   The row says `committed` because nobody can see it unless the branch commits. `effect` holds what the Saga
   needs to undo it; [its contract](lab5-saga-contract.md#effect-and-compensation-per-participant) lists the fields.
3. Answer `commit` only after `PREPARE TRANSACTION` returned.

| Found on the way | Action |
| --- | --- |
| `processed_events` inserted no row | `ROLLBACK`, answer `already_applied` |
| A rule forbids it | `ROLLBACK`, answer `reject` with the code |
| The branch row exists (`23505`) | `ROLLBACK`, then read it: `committed` is a repeated prepare, answer `commit`; `rolled_back` is a late prepare of an abandoned attempt, answer `409 TRANSACTION_ABORTED` |
| Lock timeout (`55P03`) or deadlock (`40P01`) | `ROLLBACK`, answer `429 TRANSACTION_BUSY` |

The Lab 4 consumer and the branch **must collide on the business key**, not only on `eventId`, because the
event a settled transaction publishes has a new `eventId`. User Management has
`UNIQUE (event_type, idempotency_key)` for this. Tamagotchi 5.0.0 is unique on `event_id` alone and adds the
same index; its `create_all` does not alter an existing table, so it needs an explicit
`CREATE UNIQUE INDEX IF NOT EXISTS` at start-up. Battle and Monster Raid use their row's settlement status. Events that the effect produces (`tamagotchi.owner_transferred`,
`tamagotchi.leveled_up`) go to the outbox inside the branch and are published when it commits.

### Commit

1. `COMMIT PREPARED` on an autocommit connection, after `SET synchronous_commit = on`. Success: answer.
2. `42704` (no such branch): if the branch row or the business key is there, it was committed before: answer.
3. Otherwise the prepared state was lost with a failed primary. Apply the same effect in one ordinary
   transaction, with the same two inserts, and answer. If a rule now forbids it, answer
   `409 COMMIT_REJECTED`: the coordinator keeps trying and shows it in `lastError`.

### Rollback

1. `ROLLBACK PREPARED`; `42704` is fine.
2. If the branch row is there as `committed`, answer `409 TRANSACTION_ALREADY_COMMITTED`.
3. Insert the branch row as `rolled_back` (`ON CONFLICT DO NOTHING`, `lock_timeout` 2 s). It stops a prepare
   of this attempt that is still on its way. A lock timeout means that prepare is running: answer
   `429 TRANSACTION_BUSY`, and the repeated rollback finds the branch.

### Two-phase commit from the services' languages

Go (pgx): run `BEGIN … PREPARE TRANSACTION` with `Exec` on one acquired connection, and `COMMIT PREPARED`
as a single `Exec`. Python (SQLAlchemy 2.0 + psycopg 3, run against the Tamagotchi 4.0.0 image):

```python
with engine.connect() as connection, Session(bind=connection) as session:  # one connection per branch
    session.execute(text("SET LOCAL lock_timeout = '2s'"))
    session.execute(text("SET LOCAL synchronous_commit = on"))
    ...                                              # the branch row, the ledger row, the effect
    session.flush()
    session.execute(text(f"PREPARE TRANSACTION '{gid}'"))  # gid is built from a UUID and an integer only
    session.rollback()                               # already detached: this only resets the session

with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
    connection.exec_driver_sql("SET synchronous_commit = on")
    try:
        connection.exec_driver_sql(f"COMMIT PREPARED '{gid}'")   # or ROLLBACK PREPARED
    finally:
        connection.exec_driver_sql("RESET synchronous_commit")   # the connection returns to the pool
```

PostgreSQL's default `max_prepared_transactions` is 0, which refuses `PREPARE TRANSACTION`. Tests need
`postgres -c max_prepared_transactions=10`. A GitHub service container takes no command, so set it with
`ALTER SYSTEM SET max_prepared_transactions = 10` and restart the container before the tests.

## Business rules

The reward rules stay those of Lab 4; only their application becomes atomic.

| Participant | Effect | Rejects with |
| --- | --- | --- |
| User Management | `battle.settlement`: `currency.winnerDelta` and `currency.loserDelta`; `raid.payout`: each `globalCurrency`. A debit stops at 0, as today, and `effect` records the applied amount | `USER_NOT_FOUND`: no account with that ID. A deleted account still has one |
| Tamagotchi | `battle.settlement`: transfers `transferredTamagotchiId` to the winner, creates the winner's secondary reference, grants both XP shares; `raid.payout`: each `xp` | `TAMAGOTCHI_NOT_FOUND`: the transferred creature is gone; `TAMAGOTCHI_NOT_OWNED`: the loser no longer owns it, e.g. after losing it in another battle. **New**: the transfer expects the loser as the owner. Until now the consumer skipped an unknown creature and transferred from any owner. XP for a creature that is gone is still skipped |
| Battle | `settlementStatus` `pending` → `settled`; stores `battle.finished` in its outbox | `BATTLE_NOT_DECIDED`: unknown or undecided battle; `SETTLEMENT_MISMATCH`: a `transactionId` other than the one it stored with the start request, or a payload other than the stored result |
| Monster Raid | The same for the raid's payout; stores `raid.monster_defeated` in its outbox | `RAID_NOT_DEFEATED`: unknown raid or one that was not defeated; `SETTLEMENT_MISMATCH` as for Battle |

- The initiator stores the `transactionId` of its start request with the battle or raid. A final rollback marks the
  settlement `failed` only for that ID, so a wrong request for a real battle or raid cannot spoil its settlement.
- The match or the raid itself is decided by its owner before the transaction and stays decided. A rejected
  settlement means that **nobody** receives a reward and no creature changes hands.
- `Battle` gains `settlementStatus`: `null` until decided, then `pending`, `settled` or `failed`. Monster
  Raid exposes the same field on `Raid`. Both are additions; no existing field changes. A migration marks
  what was decided before the upgrade as `settled`, because its event is already on its way.
- Expired and cancelled raids, and declined and expired battles, start no transaction.
- The CSV exports of #194 list results, not rewards, so they do not depend on the settlement.

## Coordinator rules

1. **Voting.** All participants are asked at once and may be asked again until `TX_PREPARE_TIMEOUT_MS`.
2. **Decision.** All voted yes → commit. Any reject → rollback, final. Votes missing at the timeout →
   rollback of this attempt, not final (`VOTE_MISSING: <services>`), then a new attempt after a backoff (1 s
   doubling to 30 s).
3. **Durable point.** The commit decision is journaled and synced **before** the first commit is sent. A
   transaction without a journaled commit decision is aborted (presumed abort).
4. **Commit order.** The initiator commits last, when all others have confirmed. Its outbox event is
   therefore published only after every reward is in place.
5. **No giving up.** Commit and a final rollback are repeated until every participant confirmed, with a
   backoff of 1 s doubling to 30 s, also across Broker restarts. No phase counts as delivered without its
   `200`. The rollback of an attempt that is not the last waits at most `TX_PREPARE_TIMEOUT_MS` for each
   participant: one that is still unreachable, usually the one whose vote was missing, is left to the
   recovery scan (rule 7), so the transaction reaches `waiting` while that participant is down.
6. **Restart.** `committing` and `rolling_back` continue; `preparing` rolls its attempt back
   (`COORDINATOR_RESTARTED`) and starts the next one.
7. **Recovery scan.** Every `TX_RECOVERY_INTERVAL_MS` the coordinator reads each participant's `/prepared`
   list. A branch whose attempt was decided to commit gets `commit`, also after the transaction finished; one
   that is still voting, or whose decision its participant has not confirmed yet, is left to the
   transaction itself; any other gets `rollback`. This resolves branches that reappear on a promoted database.

| Broker setting | Default | |
| --- | --- | --- |
| `TX_PREPARE_TIMEOUT_MS` | 10000 | Voting window of one attempt |
| `TX_RETRY_BACKOFF_MS` | 1000 | First delay before a phase or an attempt is repeated; doubles up to 30 s |
| `TX_MAX_ACTIVE` | 32 | Transactions in progress at once; the rest wait in the journal |
| `TX_RECOVERY_INTERVAL_MS` | 30000 | |
| `TX_RETENTION_HOURS` | 168 | How long finished transactions stay readable |
| `TX_PAUSE_POINT`, `TX_PAUSE_MS` | `none`, 0 | Rehearsals only: `before-decision` or `after-decision` holds the coordinator there, to kill it at a known moment |

Metrics: `broker_transactions_total{operation,outcome}`, `broker_transactions_active{status}`,
`broker_transaction_duration_seconds{operation}`, `broker_transaction_phases_total{service,phase,outcome}`. Logs: `transaction.started`, `.voted`, `.decided`,
`.completed`, `.retrying`, `.recovered`, with `transactionId`, `businessKey` and `attempt`.

## Database replication and promotion

`scripts/ha/postgres-start.sh` sets, on every node of the shared template (#193):

| Setting | Why |
| --- | --- |
| `max_prepared_transactions=64` | Allows branches. A standby needs at least the primary's value |
| `synchronous_standby_names='ANY 1 (*)'` and `synchronous_commit=local` | Ordinary commits stay asynchronous. A branch sets `synchronous_commit = on`, so a vote and a commit are confirmed only after one standby has them |

The promotion script refuses a standby that is behind the other one, so a confirmed branch survives the
loss of the primary. With **no** standby running, a branch waits for one to return: it casts no vote, and
ordinary traffic is not affected. Prepared branches are replicated, stay in doubt on the promoted node and
are resolved there by the coordinator's repeated commit or its recovery scan.

Checked on PostgreSQL 17 with this template: a branch prepared on the primary was in doubt on the promoted
standby and committed there; a second insert of its ledger key waited and ended with `55P03`; a repeated
commit returned `42704`; a prepare after a `rolled_back` row failed with `23505`; with both standbys stopped
an ordinary update took 0.1 s while `PREPARE` waited and returned when one standby came back.
`scripts/ha/check-replication.sh` passes with these settings.

A branch holds its row locks until it is resolved. While the coordinator is down, other writes to the same
balance or creature wait; that cost of two-phase commit is what #197 removes.

## Legacy events and cutover (the ledger is kept by Saga)

- Battle and Monster Raid 5.x store a start request in the outbox **instead of** the completion event; the
  same relay posts it to `/api/v1/transactions` until it gets `200` or `202`.
- `battle.finished` and `raid.monster_defeated` keep their topic, version and payload. Notification needs
  no change and now hears only final results.
- User Management and Tamagotchi keep their Lab 4 event consumers. An event queued by a 4.x producer is
  applied once, and later attempts to settle that battle answer `already_applied`. The event a settled
  transaction publishes finds its ledger key and is a `duplicate`.
- Repeating the start request, a phase or the event therefore never applies an effect twice.

Rollout order: the database template, then User Management and Tamagotchi, then the Broker, then Battle
and Monster Raid. Before going back to a 4.x producer, wait until no transaction is active.

## Work per owner

| Owner | Work for #196 |
| --- | --- |
| @vladas9 | Coordinator in the Broker; participant and initiator in Battle; participant in User Management; database template settings |
| @CyberMoonie | Participant in Tamagotchi: the four routes, `transaction_branches`, the branch effect sharing code with the `battle.finished`/`raid.monster_defeated` consumer, tests against PostgreSQL with prepared transactions |
| @Nickseen | Participant and initiator in Monster Raid; `settlementStatus` on `Raid`; the database promotion scenario |
| @gabimiric | The scenarios below as scripts with recorded results; the checkpoint with exact revisions, configuration and commands |

A participant can be built and tested without the coordinator: the four routes are plain HTTP.

## Scenarios to record

| # | Scenario | Expected |
| --- | --- | --- |
| 1 | A battle to knockout | `committed`; balances, owner, secondary reference and XP changed once; `battle.finished` published after that |
| 2 | Two battles with the same primary, the second settles after the creature changed owner | `rolled_back`, `TAMAGOTCHI_NOT_OWNED`; no balance, XP or owner changed; `settlementStatus` `failed`; no event |
| 3 | A defeated raid | `committed`; every reward paid once |
| 4 | `raid.payout` for a raid that is still active (posted by hand) | `rolled_back`, `RAID_NOT_DEFEATED`; nothing changed |
| 5 | Broker killed at `before-decision` | After restart attempt 1 is rolled back and attempt 2 commits |
| 6 | Broker killed at `after-decision` | After restart the commit continues; no branch stays in doubt |
| 7 | A participant killed after its vote | Its other replica, or the restarted one, commits the branch |
| 8 | A participant down for longer than the voting window | `waiting`, no locks held; commits once it returns |
| 9 | Primary lost and a standby promoted while a branch is in doubt | The branch is resolved on the promoted node; `/prepared` is empty afterwards |
| 10 | The start request and a commit sent again | Same transaction, nothing applied twice |
| 11 | A `battle.finished` queued by a 4.x producer, then a settlement of the same battle | Applied once; the vote is `already_applied` |

For every run, check afterwards that `SELECT gid FROM pg_prepared_xacts` is empty in each participant's
database, and that every balance, XP value and owner involved matches the expected outcome.

**Checkpoint.** The verified 2PC code is published as released `5.x` images with immutable Git tags before
#197 starts, so it stays runnable for the defense whatever the later squash merges do.
