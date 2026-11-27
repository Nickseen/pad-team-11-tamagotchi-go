# Lab 5: two-phase commit scenarios (#196)

The [2PC checkpoint](lab5-2pc-checkpoint.md) records the verified release pins and all 11 results.

The scenarios of the [transaction contract](lab5-transaction-contract.md#scenarios-to-record) as one
script, [`scripts/tx/check-2pc.py`](../scripts/tx/check-2pc.py), with the shared helpers in
[`scripts/tx/txstack.py`](../scripts/tx/txstack.py) that the Saga scenarios of #197 reuse. Python 3.10+,
standard library only.

```sh
docker compose up -d
python scripts/tx/check-2pc.py --preflight     # which components serve 2PC yet
python scripts/tx/check-2pc.py                 # scenarios 1-8, 10, 11
python scripts/tx/check-2pc.py --scenario 9 --allow-promotion
```

Every scenario creates its own users, creatures, battles and raids through the Broker, as the Gateway
forwards client requests, so runs do not depend on each other or on existing data. A run writes
`docs/lab5-2pc-runs/<time>.md` and `.json` with every check, the CPR commit, the submodule commits, each
image with its ID and revision label, and the Broker's `TX_*` settings: that file is the checkpoint
evidence for #196 and the defense. The script refuses to run until the preflight passes (the Broker's
transaction routes, every participant's `/prepared` route and `max_prepared_transactions > 0`).

## How each scenario is driven

After every scenario the script checks that `pg_prepared_xacts` is empty in all four participant
databases (on the current writer), and records each participant's `transaction_branches` rows.

| # | Scenario | How the script produces it | Checked |
| --- | --- | --- | --- |
| 1 | A battle to knockout | Two friends play `attack` turns until one is knocked out | `committed`, attempt 1, all participants `committed`; loser's primary owned by the winner with `capturedInBattleId`; one new secondary reference; winner's balance up, loser's down or clamped at 0, equal to User Management's recorded `effect`; both primaries gained XP; `settlementStatus` `settled`; one `battle.finished`; nothing changes 5 s later |
| 2 | Same primary, second settlement after the owner changed | The loser accepts two challenges with the same primary, then forfeits both | First `committed`; second `rolled_back` with `TAMAGOTCHI_NOT_OWNED`; balances, XP and owners as after the first; second `settlementStatus` `failed`; one `battle.finished` |
| 3 | A defeated raid | A guild of two joins a raid on a 30 HP monster and attacks until it is defeated | `committed`; both balances and primaries' XP up; `settlementStatus` `settled`; one `raid.monster_defeated`; nothing changes 5 s later |
| 4 | `raid.payout` for an active raid | `POST /api/v1/transactions` by hand for a raid nobody attacked | `202`, then `rolled_back` with `RAID_NOT_DEFEATED`; nothing changed; raid still `active` |
| 5 | Broker killed at `before-decision` | Broker with `TX_PAUSE_POINT=before-decision`; killed once all three participants list the branch in `/prepared`; restarted with its own settings | `committed` at attempt 2; attempt 1 `rolled_back` in User Management; the effects of scenario 1 once |
| 6 | Broker killed at `after-decision` | The same at `after-decision`, once the status is `committing` | `committed` at attempt 1; the effects once; no branch in doubt |
| 7 | A participant killed after its vote | Paused at `before-decision` for 20 s; one Tamagotchi replica killed once every branch is prepared, started again afterwards | `committed`; the effects once |
| 8 | A participant down longer than the voting window | Use a 20 s retry delay, stop all Tamagotchi replicas before the forfeit, and check both databases while the same attempt is `waiting`. Start the replicas after 15 s | `waiting` with `VOTE_MISSING: tamagotchi`; no prepared branch in User Management or Battle meanwhile; `committed` at attempt ≥ 2; the effects once |
| 9 | Promotion with a branch in doubt | Pause at `after-decision`, verify both Tamagotchi standbys replayed the WAL through the prepare, kill `tamagotchi-db`, and promote the most caught-up standby with `postgres-promote.sh` | The branch is in doubt on the promoted node, then `committed` there; Tamagotchi `/prepared` empty; the effects once |
| 10 | Start request and commit sent again | After a committed forfeit: the start request again with the same `transactionId`, again with a new one, and `commit` again to each participant | `200` and the same `transactionId` both times; each commit `200 committed`; one transaction for the battle; nothing applied twice; one `battle.finished` |
| 11 | Queued 4.x event, then the settlement | `TX_RETRY_BACKOFF_MS=20000`; User Management and Tamagotchi stopped; forfeit; once `waiting`, a `battle.finished` published as Battle 4.x does (+250/−100); both started | `committed`; balances moved by the event's amounts exactly once; owner and secondary reference once; no committed branch in User Management or Tamagotchi (`already_applied` prepares nothing); two `battle.finished` (the queued one and Battle's), the second a duplicate |

Scenarios 5–9 and 11 recreate the Broker with rehearsal settings through a temporary Compose
override and restore its settings even after a failure. Publication counters are scoped to a single
Broker process and reset when it restarts. In scenario 9, the WAL replay check precedes the promotion
and `pg_prepared_xacts` on the new writer confirms the branch. `--allow-promotion` enables this
scenario. The old primary stays fenced until the README's rejoin procedure is run.

## Baseline before 2PC

`--setup-only` runs the scenarios' battle, raid and second-owner steps without any 2PC check. On the
Lab 4/5.0 images (Battle 4.1.0, User Management 4.0.2, Tamagotchi 5.0.0, Monster Raid 4.0.0, Broker
4.0.0) on 2026-11-25: a battle to knockout took 19 turns and the Lab 4 consumers paid it; a raid on the
30 HP monster was defeated. With the setup of scenario 2, the second forfeit moved the creature a
**second time**, from the first winner to the second one, because the Lab 4 consumer transfers from any
owner. That is the partial, inconsistent outcome scenario 2 must show disappearing: with 2PC the second
settlement is rolled back with `TAMAGOTCHI_NOT_OWNED` and nobody is paid.
