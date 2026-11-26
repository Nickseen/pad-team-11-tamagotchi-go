# Lab 5: long-running saga contract

Status: **draft** for CPR #197. It becomes binding when its PR is approved; implementations start after the 2PC
checkpoint of #196. Published early so that the participants of #196 store what compensation needs.

The Saga replaces the two phases of the [transaction contract](lab5-transaction-contract.md). What that document
marks *kept by Saga* stays: the identifiers, the start request and `GET` routes of the Broker, the payloads, and
the ledger that makes events and transactions exclude each other. The Message Broker remains the coordinator.

| | Two-phase commit (#196) | Saga (#197) |
| --- | --- | --- |
| Participant's local transaction | Prepared, decided later | Committed at once |
| Locks between the phases | Held | None |
| Others can see a half-done operation | No | Yes, until it ends |
| Undo | `ROLLBACK PREPARED` by the database | A compensation the service writes itself |
| Outage of a participant | Attempt rolled back, next attempt | The saga waits at that step |

## Steps

Steps run one after another, each in one participant's own database transaction.

| # | Step | `battle.settlement` | `raid.payout` | Kind | Compensation |
| --- | --- | --- | --- | --- | --- |
| 1 | `verify` | Battle | Monster Raid | Check | Mark the settlement `failed` |
| 2 | `currency` | User Management | User Management | Compensatable | Take the credits back, return the debit |
| 3 | `creatures` | Tamagotchi | Tamagotchi | **Pivot** | Return the creature, take the XP back |
| 4 | `complete` | Battle | Monster Raid | Retriable | — |

- **`verify`** changes nothing. It confirms that the request is the initiator's own transaction for a result it stored
  and that is still `pending`, so a wrong request pays nobody (see [The initiator's own transaction](#the-initiators-own-transaction)).
- **`currency`** comes before `creatures` because money is simpler to give back than a creature.
- **`creatures`** is the point of no return: the last step with a business rule that can refuse. Once it is done the
  saga only goes forward.
- **`complete`** marks the settlement `settled` and stores `battle.finished` or `raid.monster_defeated` in the
  initiator's outbox. It cannot be refused after `verify`, so the result is announced exactly when it is final.

The compensation of `creatures` is needed only when `complete` fails for good. In a normal run that cannot happen
after `verify`; a rehearsal forces it with `TX_REJECT_STEP`, which is the one exception to "after the pivot only
forward". It is part of the contract and of the acceptance checks.

## Coordinator

| `status` | Meaning |
| --- | --- |
| `executing` | Running the steps in order |
| `compensating` | Undoing what was done, in reverse order |
| `completed`, `compensated` | Final |

1. **Order.** A step is sent only after the previous one is journaled as done, so progress survives a restart.
2. **Answers.** Only a `200` is one. Anything else is repeated with a backoff of 1 s doubling to 30 s. No lock is held
   meanwhile. The one exception is `409 TRANSACTION_ABORTED` to an `execute`: that step was already compensated or
   resolved as aborted, so it counts as `compensated` and is not sent again.
3. **Refusal.** `rejected` from a step journals `compensating` with the code as `reason`, before the first
   compensation is sent.
4. **Deadline.** A saga that has not passed the pivot after `TX_SAGA_DEADLINE_MS` is compensated with
   `DEADLINE_EXCEEDED: <step>`, after its step in flight is resolved (rule 5). After the pivot there is no deadline. A
   saga compensated at its deadline is never paid later, because rule 6 leaves the business key in every participant;
   that is the price of not waiting for ever.
5. **Unknown outcome.** Before the coordinator chooses between compensation and the next step, a step whose answer it
   never got is resolved with `resolve`: the participant reports `executed`, or makes the step impossible and reports
   `aborted`. An `executed` pivot means the saga passed the point of no return, so it continues with `complete`; an
   `executed` step before the pivot is compensated with the others; `aborted` leaves nothing to undo.
6. **Compensation** goes in reverse order and ends with `verify`. It is sent to **every** step except the
   `already_applied` ones: executed, refused, resolved or never sent. A participant whose step did not run records the
   business key as finished, so no queued Lab 4 event pays there later. Compensations are repeated until each answers
   `200` and cannot be refused. Once a step's compensation is journaled, its `execute` is never sent again.
7. **Restart.** The Broker continues at the first step, or the first compensation, that is not journaled as done; a
   step that was in flight is resolved first.

`GET /api/v1/transactions/{id}` adds `protocol` (`2pc` or `saga`) and, for a saga, `steps`:

```json
{
  "transactionId": "5d0c1f0e-…", "protocol": "saga", "operation": "battle.settlement",
  "status": "compensating", "reason": "TAMAGOTCHI_NOT_OWNED",
  "steps": [
    { "step": "verify", "service": "battle", "state": "executed", "lastError": null },
    { "step": "currency", "service": "user-management", "state": "compensated", "lastError": null },
    { "step": "creatures", "service": "tamagotchi", "state": "rejected", "lastError": null },
    { "step": "complete", "service": "battle", "state": "pending", "lastError": null }
  ]
}
```

Step states: `pending`, `executed`, `already_applied`, `rejected`, `aborted`, `compensated`.

| Broker setting | Default | |
| --- | --- | --- |
| `TX_PROTOCOL` | `saga` | Protocol of **new** transactions: `saga` or `2pc`. Each keeps the one it started with |
| `TX_SAGA_DEADLINE_MS` | 86400000 | Until the pivot; 24 hours, so an outage delays a payout and rarely fails it |
| `TX_REJECT_STEP` | `none` | **Rehearsals only.** The Broker treats this step as refused with `REHEARSAL` instead of sending it, also `complete`, which overrides the rule that nothing is compensated after the pivot. The Broker logs a warning at start while it is set |
| `TX_PAUSE_POINT`, `TX_PAUSE_MS` | `none`, 0 | Also `after-step:<step>` and `before-compensation:<step>` |

Metrics and logs are those of 2PC with the saga's outcomes, plus `broker_saga_steps_total{step,outcome}` and
`transaction.step`, `transaction.compensating`.

## Participant API

| Route | `200` answer |
| --- | --- |
| `POST /api/v1/internal/transactions/{transactionId}/execute` | `{ "result": "executed" }`, `{ "result": "already_applied" }` or `{ "result": "rejected", "code": "…", "message": "…" }` |
| `POST /api/v1/internal/transactions/{transactionId}/compensate` | `{ "status": "compensated" }` |
| `POST /api/v1/internal/transactions/{transactionId}/resolve` | `{ "state": "executed" }` or `{ "state": "aborted" }` |

`execute` answers `409 TRANSACTION_ABORTED` for a step that was compensated or resolved as aborted; nothing is applied.

```json
{
  "transactionId": "5d0c1f0e-…", "step": "currency",
  "operation": "battle.settlement", "operationVersion": 1, "businessKey": "3c9e1f70-…",
  "payload": { }, "reason": "TAMAGOTCHI_NOT_OWNED"
}
```

`reason` is sent with `compensate` only; `resolve` takes the same body without `payload`. Headers and authentication are those of the 2PC phases. The 2PC routes stay
until no 2PC transaction is left (see Cutover).

## What a participant does in PostgreSQL

The ledger is the table of #196 with two outcomes and one column more:

```sql
ALTER TABLE transaction_branches
    DROP CONSTRAINT transaction_branches_outcome_check,
    ADD CONSTRAINT transaction_branches_outcome_check
        CHECK (outcome IN ('committed', 'rolled_back', 'executed', 'compensated')),
    ADD COLUMN compensation JSONB;  -- what the compensation really undid
```

A saga uses `attempt` 0, so its rows never meet those of a 2PC attempt. The initiator has one row for `verify` and
`complete`: `complete` writes it as `executed`, the compensation of `verify` as `compensated`. A service that creates
its tables with `create_all` needs explicit `ALTER` statements for an existing one.

**Execute**, in one ordinary transaction with `lock_timeout` 2 s and `synchronous_commit = on`:

1. Insert the row as `executed`, `ON CONFLICT DO NOTHING`. If it is there: `executed` → answer `executed`;
   `compensated` → answer `409 TRANSACTION_ABORTED`.
2. Insert the event's ledger key. If it is there: roll back, answer `already_applied`.
3. Check the rules; a refusal rolls back and answers `rejected`.
4. Apply the effect and write **everything needed to undo it** into `effect`.

**Compensate**, in one ordinary transaction with the same settings:

1. Lock the row. `compensated` → answer.
2. Missing: the step did not run, or it was refused. Insert the row as `compensated` and the event's ledger key,
   `ON CONFLICT DO NOTHING`, and answer. If the key was already there, an event applied the effect earlier: it is not
   undone, and `compensation` records `{ "alreadyApplied": true }`. The initiator marks its settlement `failed` here,
   for its own transaction only.
3. `executed`: undo `effect`, write what was really undone into `compensation`, set `compensated`, answer.

**Resolve**, in one ordinary transaction with the same settings: an `executed` row answers `executed`; otherwise do
what compensate does for a missing row and answer `aborted`.

The ledger key stays after a compensation, and a step that never ran gets one, so no late event pays a failed battle or
raid in any participant. Both can be repeated any
number of times. A lock timeout answers `429 TRANSACTION_BUSY`. A compensation is never refused: what can no longer
be undone is recorded in `compensation` and logged as `transaction.compensation_incomplete`.

### Effect and compensation per participant

| Participant | `effect` | Compensation | When the world moved on |
| --- | --- | --- | --- |
| User Management | `{ "balances": [{ "userId", "delta" }] }` with the **applied** deltas (stored since 5.1.0) | Applies `-delta` to each | A credit already spent: the balance stops at 0 and the shortfall is recorded |
| Tamagotchi | `{ "transfer": { "tamagotchiId", "previousOwnerId", "wasPrimary", "previousCapturedInBattleId", "secondaryReferenceId" }, "xp": [{ "tamagotchiId", "amount" }] }` | Gives the creature back with its previous `captured_in_battle_id`, makes it primary again if the owner has no primary, deletes the secondary reference, takes each XP amount back | The creature changed owner again or is gone: nothing is transferred and it is recorded |
| Battle, Monster Raid | — | `verify`: settlement `pending` → `failed`, for the initiator's own transaction | Already `settled`: cannot happen, because nothing is compensated once `complete` ran; answers `compensated` and logs an error |

Taking XP back is exact and independent of other grants, because a level costs a fixed amount:

```python
tamagotchi.xp -= amount
while tamagotchi.xp < 0:              # the levels this grant paid for
    tamagotchi.level -= 1
    tamagotchi.xp += XP_PER_LEVEL * tamagotchi.level
```

The `xp_operations` row stays, so the grant cannot be repeated. Compensations publish no event.

**Store these effects from #196 on**, in the branch: then 2PC and Saga share one function that applies the effect.

## The initiator's own transaction

Battle and Monster Raid store the `transactionId` of their start request with the battle or raid, in the same local
transaction as the outbox row. `verify` rejects any other ID with `SETTLEMENT_MISMATCH`, as it does a payload that is
not the stored result. The compensation of `verify` marks the settlement `failed` only for the stored ID. A wrong
request for a real battle or raid therefore neither pays anyone nor spoils the real settlement. The same binding
applies to two-phase commit (see the [transaction contract](lab5-transaction-contract.md#business-rules)).

## Refusals

The codes are those of #196. A refusal at `verify` (`BATTLE_NOT_DECIDED`, `SETTLEMENT_MISMATCH`,
`RAID_NOT_DEFEATED`) undoes nothing, but every participant is told and records the key. `USER_NOT_FOUND` at
`currency` and `TAMAGOTCHI_NOT_FOUND` or `TAMAGOTCHI_NOT_OWNED` at `creatures` undo every executed step.

## What others can see meanwhile

Sagas have no isolation. Between `currency` and `complete` the winner's balance already shows the reward and the
creature may already belong to the winner, while `settlementStatus` is `pending`. Countermeasures in this contract:
the order of the steps, deltas instead of absolute values, checks by owner before undoing, `settlementStatus` as the
visible marker, and the result event only at the end. Clients show a reward as final when the status is `settled` or
the notification arrives.

## Cutover from 2PC

1. Release the participants that serve both protocols: User Management, Tamagotchi, Battle, Monster Raid.
2. Release the Broker with `TX_PROTOCOL=saga`. Every transaction in its journal keeps its protocol: an unfinished 2PC
   transaction finishes as 2PC. Records without the field are `2pc`.
3. The start request is unchanged, so requests waiting in an outbox become sagas.
4. Events queued by a Lab 4 producer still apply once: they share the ledger key with both protocols.
5. When `GET /api/v1/transactions?status=` shows no active 2PC transaction and `/prepared` is empty everywhere, the 2PC
   routes may be removed in a later release. They stay for the defense.

Going back is `TX_PROTOCOL=2pc`; running sagas finish as sagas.

The 2PC checkpoint stays runnable from its released images and tags, whatever this changes.

## Work per owner

| Owner | Work for #197 |
| --- | --- |
| @vladas9 | Saga coordinator, its journal and settings; `currency` in User Management; `verify` and `complete` in Battle |
| @CyberMoonie | `creatures` in Tamagotchi with both compensations |
| @Nickseen | `verify` and `complete` in Monster Raid; recovery across a database promotion |
| @gabimiric | The scenarios below with recorded results, next to those of #196 |

## Scenarios to record

| # | Scenario | Expected |
| --- | --- | --- |
| 1 | A battle to knockout; a defeated raid | `completed`; the business outcome of 2PC scenarios 1 and 3 |
| 2 | Two battles with the same primary, the second settles after the creature changed owner | `creatures` rejected; `currency` and `verify` compensated; balances as before; `failed`; no event; Tamagotchi holds the business key |
| 3 | A wrong request posted by hand (2PC scenario 4), and one with a real battle's key and payload but another ID | `verify` rejected; nothing changed; the real settlement still `pending` and completes |
| 4 | `TX_REJECT_STEP=complete` for a battle and for a raid | Currency, XP, level and owner as before; the secondary reference gone |
| 5 | Broker killed after `currency`, and after `creatures` | Continues at the next step; nothing applied twice |
| 6 | Broker killed in the middle of a compensation | Continues; nothing undone twice |
| 7 | A participant away at its step, longer than the 2PC voting window | `executing`, no locks; completes when it returns |
| 8 | The same with a short `TX_SAGA_DEADLINE_MS`, before the pivot and after it | Compensated, the business key recorded everywhere; after the pivot it completes |
| 9 | Primary lost and a standby promoted between two steps | Completes on the promoted node |
| 10 | Every execute and compensate sent twice | Nothing changes the second time |
| 11 | The winner spends the reward before the compensation | Balance 0, shortfall recorded |
| 12 | Cutover with a 2PC transaction in progress, a request in an outbox and a queued Lab 4 event | Each battle paid once, announced once |
| 13 | `compensate` arrives before its `execute` (the late `execute` posted by hand) | `409 TRANSACTION_ABORTED`; nothing applied |
| 14 | The answer of `creatures` lost at the deadline, after Tamagotchi committed it | `resolve` reports `executed`; the saga completes and nothing is compensated |
| 15 | A queued Lab 4 event for a battle whose saga was compensated after a refusal at `verify` | A duplicate in User Management and Tamagotchi; nothing paid |
