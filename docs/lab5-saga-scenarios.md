# Lab 5: Saga scenarios (#197)

The scenarios of the [Saga contract](lab5-saga-contract.md#scenarios-to-record) as one script,
[`scripts/tx/check-saga.py`](../scripts/tx/check-saga.py). It reuses the fixtures and checks of the
[2PC scenarios](lab5-2pc-scenarios.md) and the shared helpers in [`scripts/tx/txstack.py`](../scripts/tx/txstack.py).

```sh
docker compose up -d
python scripts/tx/check-saga.py --preflight     # the Broker and every participant serve the Saga routes
python scripts/tx/check-saga.py                 # scenarios 1-8, 10-15
python scripts/tx/check-saga.py --scenario 9 --allow-promotion
```

A saga is final when `GET /api/v1/transactions/{id}` shows `completed` or `compensated`. The runner
reads each step's state from that view. It counts results where they survive a Broker restart:
- **Balances, creatures and secondary references** through the services.
- **Ledger keys** (`processed_events`) and **branches** (`transaction_branches`, attempt 0) in each
  participant's database.
- **Announcements** as the distinct `battle.finished` or `raid.monster_defeated` events Notification
  received for the battle or raid. The Broker's publication counter restarts with it.

The saga view leaves out the start request's `payload`. Scenarios that resend a step (3, 10, 12 and 14)
therefore read the exact stored payload from the Broker's journal (`/data/transactions.log`).

## How each scenario is driven

After every scenario the runner checks that `pg_prepared_xacts` is empty in all four participant
databases.

| # | Scenario | How the script produces it | Checked |
| --- | --- | --- | --- |
| 1 | A battle to knockout; a defeated raid | Attacks until a knockout; a guild of two defeats a 30 HP monster | `completed`, every step `executed`; owner, `capturedInBattleId`, one new secondary reference, balances equal to User Management's recorded `effect`, XP up; `settled`; one announcement each; nothing changes 5 s later |
| 2 | Same primary, second settlement after the owner changed | The loser accepts two challenges with one primary and forfeits both | First `completed`; second `compensated` with `TAMAGOTCHI_NOT_OWNED`, `verify` and `currency` compensated; everything as after the first; `failed`; no announcement; Tamagotchi holds the second battle's key |
| 3 | A wrong request, and a real battle's key and payload under another ID | **(a)** A `raid.payout` for an active raid through the Broker. **(b)** With the real saga held after `verify`, the same battle under another ID: through the Broker, then straight to Battle's `verify`, then its compensation | **(a)** `compensated` with `RAID_NOT_DEFEATED`; nothing changed. **(b)** The Broker answers `200` with the real saga; Battle rejects `SETTLEMENT_MISMATCH`; the real settlement stays `pending`, then completes |
| 4 | `TX_REJECT_STEP=complete` | A forfeited battle and a defeated raid under the rehearsal setting | `compensated` with `REHEARSAL`; balances, XP, levels and owners as before; secondary reference gone; the loser's primary restored; `failed`; no announcement; key in both ledgers |
| 5 | Broker killed after `currency`, and after `creatures` | Held with `TX_PAUSE_POINT=after-step:<step>`, killed, restarted with its own settings | Both `completed`, every effect once |
| 6 | Broker killed mid-compensation | `TX_REJECT_STEP=complete`, held at `before-compensation:currency` after `creatures` was undone, killed, restarted | `compensated`, everything as before, one Tamagotchi branch row |
| 7 | A participant away longer than the 2PC voting window | Tamagotchi stopped before the forfeit; 15 s after `currency` | `executing`, `creatures` `pending` (`no ready tamagotchi instance`); no prepared transaction and no session idle in a transaction; `completed` once it returns |
| 8 | Short deadline, before and after the pivot | **(a)** `TX_SAGA_DEADLINE_MS=15000` with Tamagotchi away for 25 s. **(b)** `TX_SAGA_DEADLINE_MS=5000`, held 15 s after `creatures` | **(a)** `compensated` with `DEADLINE_EXCEEDED: creatures`, everything as before, keys recorded. **(b)** `completed` |
| 9 | Promotion between two steps | Held after `currency`; the Tamagotchi writer killed and the most caught-up standby promoted | `completed` on the promoted node, with its `creatures` branch `executed` there |
| 10 | Every execute and compensate sent twice | Straight to the participants, after a `completed` saga and after a `compensated` one | `200` both times; balances, XP, owners and branch rows unchanged; one announcement |
| 11 | The winner spends the reward before the compensation | `TX_REJECT_STEP=complete`, held at `before-compensation:currency`; the winner's whole balance spent through User Management's adjust route | `compensated`; winner at 0; `shortfall` in User Management's `compensation`; the creature back with the loser |
| 12 | Cutover | A 2PC transaction (`TX_PROTOCOL=2pc`) held before its decision. The Broker killed; two battles decided straight at Battle, so their start requests wait in its outbox. The Broker restarted as Saga, held after `verify`; a Lab 4 `battle.finished` published for the third battle | The 2PC transaction `committed` as 2PC; both outbox requests `completed` as sagas; the third with `currency` and `creatures` `already_applied`; every winner paid once; one announcement per battle (two for the third: the Lab 4 event and Battle's) |
| 13 | `compensate` before `execute` | Straight to User Management and Tamagotchi for a battle nobody else settles | `compensate` `200`; the late `execute` `409 TRANSACTION_ABORTED`; nothing applied; key recorded |
| 14 | The answer of `creatures` lost at the deadline | `TX_SAGA_DEADLINE_MS=25000`, held after `currency`. `creatures` executed straight at Tamagotchi, then Tamagotchi killed past the deadline and started again | The Broker logs `phase: resolve` for `creatures`; `completed` with nothing compensated; every effect once |
| 15 | A queued Lab 4 event after a refusal at `verify` | A settlement for an accepted, undecided battle through the Broker; then a Lab 4 `battle.finished` for it | `compensated` with `BATTLE_NOT_DECIDED`; key in both ledgers; the event changes nothing |

The runner recreates the Broker with each rehearsal setting through a temporary Compose override and
restores it afterwards. Scenario 9 needs `--allow-promotion`. Afterwards, rejoin the old writer as in the
README before running it again; the runner promotes from whichever node is the writer.
