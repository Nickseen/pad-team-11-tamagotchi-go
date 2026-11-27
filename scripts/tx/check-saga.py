#!/usr/bin/env python3
"""Saga scenarios of #197 (docs/lab5-saga-contract.md, "Scenarios to record").

Runs against the stack started from this checkout (docker compose up -d) with the Broker published on
127.0.0.1:8091, like check-2pc.py, whose fixtures and checks it reuses. A saga is final when its status is
`completed` or `compensated`; its steps are read from GET /api/v1/transactions/{id}.

  python scripts/tx/check-saga.py --list
  python scripts/tx/check-saga.py --preflight
  python scripts/tx/check-saga.py                       # scenarios 1-8, 10-15
  python scripts/tx/check-saga.py --scenario 9 --allow-promotion

Results are counted where they survive a Broker restart: balances and creatures through the services,
ledger keys and branches in each participant's database, and announcements as the distinct result events
Notification received for the battle or raid (the Broker's publication counter restarts with it).

Scenarios 4-9, 11, 12 and 14 recreate the Broker with rehearsal settings (TX_REJECT_STEP,
TX_SAGA_DEADLINE_MS, TX_PAUSE_POINT=after-step:<step> | before-compensation:<step>, TX_PAUSE_MS,
TX_PROTOCOL) through a temporary Compose override and restore its own settings afterwards. 7, 8 and 14
stop Tamagotchi and start it again; 12 stops the Broker while Battle decides two battles. Scenario 9
promotes a Tamagotchi standby and runs only with --allow-promotion. Results: docs/lab5-saga-runs/.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from txstack import (DATABASES, ROOT, Player, Result, Snapshot, StepFailed, new_id, now,  # noqa: E402
                     revisions, write_report)

_spec = importlib.util.spec_from_file_location("check_2pc", HERE / "check-2pc.py")
_twopc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_twopc)

SCENARIOS = {
    1: ("A battle to knockout; a defeated raid", "completed; the business outcome of 2PC scenarios 1 and 3"),
    2: ("Two battles with the same primary, the second settles after the creature changed owner",
        "creatures rejected; currency and verify compensated; balances as before; failed; no event; "
        "Tamagotchi holds the business key"),
    3: ("A wrong request posted by hand, and one with a real battle's key and payload but another ID",
        "verify rejected; nothing changed; the real settlement still pending and completes"),
    4: ("TX_REJECT_STEP=complete for a battle and for a raid",
        "currency, XP, level and owner as before; the secondary reference gone"),
    5: ("Broker killed after currency, and after creatures", "continues at the next step; nothing applied twice"),
    6: ("Broker killed in the middle of a compensation", "continues; nothing undone twice"),
    7: ("A participant away at its step, longer than the 2PC voting window",
        "executing, no locks; completes when it returns"),
    8: ("The same with a short TX_SAGA_DEADLINE_MS, before the pivot and after it",
        "compensated, the business key recorded everywhere; after the pivot it completes"),
    9: ("Primary lost and a standby promoted between two steps", "completes on the promoted node"),
    10: ("Every execute and compensate sent twice", "nothing changes the second time"),
    11: ("The winner spends the reward before the compensation", "balance 0, shortfall recorded"),
    12: ("Cutover with a 2PC transaction in progress, a request in an outbox and a queued Lab 4 event",
         "each battle paid once, announced once"),
    13: ("compensate arrives before its execute (the late execute posted by hand)",
         "409 TRANSACTION_ABORTED; nothing applied"),
    14: ("The answer of creatures lost at the deadline, after Tamagotchi committed it",
         "resolve reports executed; the saga completes and nothing is compensated"),
    15: ("A queued Lab 4 event for a battle whose saga was compensated after a refusal at verify",
         "a duplicate in User Management and Tamagotchi; nothing paid"),
}
STEPS = ["verify", "currency", "creatures", "complete"]
STEP_SERVICE = {
    "battle.settlement": {"verify": "battle", "currency": "user-management", "creatures": "tamagotchi",
                          "complete": "battle"},
    "raid.payout": {"verify": "monster-raid", "currency": "user-management", "creatures": "tamagotchi",
                    "complete": "monster-raid"},
}
SAGA_FINAL = {"completed", "compensated"}
NEEDS_RAID = {1, 3, 4}
NOTIFICATION = (["notification-db", "notification-db-1", "notification-db-2"], "notification", "notification")
SETTLE = 5


class SagaRun(_twopc.Run):
    # ------------------------------------------------------------------------------ observations
    def wait_saga(self, transaction_id: str, timeout=180) -> dict:
        return self.b.wait_transaction(transaction_id, SAGA_FINAL, timeout)

    def steps(self, tx: dict) -> dict[str, str]:
        return {s.get("step"): s.get("state") for s in tx.get("steps") or []}

    def wait_step(self, transaction_id: str, step: str, states: set[str], timeout=60) -> dict:
        end = time.time() + timeout
        last = None
        while time.time() < end:
            r = self.b.transaction(transaction_id)
            if r.status == 200:
                last = r.body
                if self.steps(last).get(step) in states:
                    return last
            time.sleep(0.3)
        raise StepFailed(f"step {step} of {transaction_id} did not reach {sorted(states)}: {json.dumps(last)}")

    def ledger_keys(self, key: str) -> dict[str, str | None]:
        """Rows holding the business key in each consumer's ledger (processed_events)."""
        return {p: self.c.query(p, f"SELECT count(*) FROM processed_events WHERE idempotency_key::text = '{key}'")
                for p in ("user-management", "tamagotchi")}

    def announced(self, event_type: str, key: str, expected: int, timeout=25) -> int:
        """Distinct result events Notification received for this battle or raid."""
        field = "battleId" if event_type == "battle.finished" else "raidId"
        sql = ("SELECT count(DISTINCT n.source_event_id) FROM notifications n JOIN processed_events p "
               f"ON p.event_id = n.source_event_id WHERE p.event_type = '{event_type}' AND n.data->>'{field}' = '{key}'")
        end, count = time.time() + timeout, -1
        while time.time() < end:
            nodes, user, db = NOTIFICATION
            for node in nodes:
                if self.c.psql(node, user, db, "SELECT pg_is_in_recovery()") == "f":
                    count = int(self.c.psql(node, user, db, sql) or 0)
                    break
            if count >= expected and expected > 0:
                break
            time.sleep(1)
        time.sleep(2 if expected else 0)
        return count

    def stored_payload(self, transaction_id: str) -> dict:
        """The payload of the start request, read from the Broker's journal: the saga view leaves it out."""
        cid = self.c.containers("message-broker")[0]
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["docker", "cp", f"{cid}:/data/transactions.log", tmp], capture_output=True,
                           env={**os.environ, "MSYS_NO_PATHCONV": "1"})
            raw = (Path(tmp) / "transactions.log").read_bytes().decode("utf-8", errors="replace")
        decoder, found = json.JSONDecoder(), None
        marker = f'"transactionId":"{transaction_id}"'
        for start in (i for i in range(len(raw)) if raw.startswith('{"op":', i)):
            if marker not in raw[start:start + 200]:
                continue
            try:
                record, _ = decoder.raw_decode(raw, start)
            except ValueError:
                continue
            payload = (record.get("transaction") or {}).get("payload")
            if payload:
                found = payload
        if found is None:
            raise StepFailed(f"no stored payload for {transaction_id} in the Broker's journal")
        return found

    def step_call(self, action: str, tx: dict, step: str, transaction_id: str | None = None,
                  payload=None, reason: str | None = None):
        operation = tx["operation"]
        body = {"transactionId": transaction_id or tx["transactionId"], "step": step, "operation": operation,
                "operationVersion": 1, "businessKey": tx["businessKey"]}
        if action != "resolve":
            if payload is None:
                payload = tx.get("payload") or self.stored_payload(tx["transactionId"])
            body["payload"] = payload
        if reason:
            body["reason"] = reason
        service = STEP_SERVICE[operation][step]
        return self.b.service("POST", service, f"/api/v1/internal/transactions/{body['transactionId']}/{action}", body)

    def primary_of(self, player: Player) -> str | None:
        r = self.b.service("GET", "tamagotchi", f"/api/v1/internal/users/{player.user_id}/primary-tamagotchi")
        return r.get("tamagotchiId") if r.status == 200 else None

    def idle_in_transaction(self) -> dict:
        sql = "SELECT count(*) FROM pg_stat_activity WHERE state LIKE 'idle in transaction%'"
        return {p: self.c.query(p, sql) for p in ("user-management", "battle")}

    # ------------------------------------------------------------------------------------ checks
    def completed_battle(self, res: Result, battle_id: str, winner: Player, loser: Player, before: Snapshot,
                         tx: dict, steps: dict | None = None, announcements: int = 1) -> None:
        res.check("protocol", "saga", tx.get("protocol"))
        res.check("status", "completed", tx.get("status"))
        res.check("steps", steps or {s: "executed" for s in STEPS}, self.steps(tx))
        time.sleep(SETTLE)
        after = Snapshot(self.b, [winner, loser])
        moved = loser.primary
        res.check("transferred creature's owner", winner.user_id, after.creature[moved]["ownerId"])
        res.check("capturedInBattleId", battle_id, after.creature[moved]["capturedInBattleId"])
        res.check("winner's new secondary references", [moved],
                  sorted(set(after.secondaries[winner.user_id]) - set(before.secondaries[winner.user_id])))
        gained = after.balance[winner.user_id] - before.balance[winner.user_id]
        res.check("winner's balance", "increased", f"{gained:+}", gained > 0)
        rows = self.branches("user-management", tx["transactionId"])
        effect = next((r["effect"] for r in rows if r["outcome"] == "executed"), None) or {}
        deltas = {d.get("userId"): d.get("delta") for d in effect.get("balances", []) if isinstance(d, dict)}
        if deltas:
            res.check("balances equal User Management's recorded effect", deltas,
                      {u: after.balance[u] - before.balance[u] for u in deltas})
        for p in (winner, loser):
            b, a = before.creature[p.primary], after.creature[p.primary]
            res.check(f"XP of {p.username}'s primary", "increased",
                      f"L{b['level']}/{b['xp']} -> L{a['level']}/{a['xp']}", (a["level"], a["xp"]) > (b["level"], b["xp"]))
        res.check("settlementStatus", "settled", self.f.get_battle(battle_id, winner.user_id).get("settlementStatus"))
        res.check("battle.finished announced", announcements, self.announced("battle.finished", battle_id, announcements))
        time.sleep(SETTLE)
        res.check("nothing changes afterwards", "same snapshot",
                  "same snapshot" if Snapshot(self.b, [winner, loser]).same(after) else "changed", None)
        self.record_branches(res, tx)

    def compensated_battle(self, res: Result, battle_id: str, winner: Player, loser: Player, before: Snapshot,
                           tx: dict, reason) -> None:
        res.check("protocol", "saga", tx.get("protocol"))
        res.check("status", "compensated", tx.get("status"))
        if callable(reason):
            res.check("reason", reason.__doc__, tx.get("reason"), reason(tx.get("reason") or ""))
        else:
            res.check("reason", reason, tx.get("reason"))
        steps = self.steps(tx)
        res.check("no step left executed", "none executed", {s: v for s, v in steps.items() if v == "executed"}
                  or "none executed", not any(v == "executed" for v in steps.values()))
        time.sleep(SETTLE)
        after = Snapshot(self.b, [winner, loser])
        res.check("balances, XP, levels, owners and secondary references", "as before",
                  "as before" if after.same(before) else json.dumps({"before": before.as_dict(), "after": after.as_dict()}),
                  None)
        res.check("loser's primary again", loser.primary, self.primary_of(loser))
        res.check("settlementStatus", "failed", self.f.get_battle(battle_id, winner.user_id).get("settlementStatus"))
        res.check("battle.finished announced", 0, self.announced("battle.finished", battle_id, 0))
        res.check("business key in each ledger", {"user-management": "1", "tamagotchi": "1"}, self.ledger_keys(battle_id))
        self.record_branches(res, tx)

    def record_branches(self, res: Result, tx: dict) -> None:
        for step in ("currency", "creatures"):
            service = STEP_SERVICE[tx["operation"]][step]
            res.note(f"{service} transaction_branches: {json.dumps(self.branches(service, tx['transactionId']))}")
        self.no_branch_in_doubt(res)

    def raid_completed(self, res: Result, players: list[Player], raid_id: str, before: Snapshot, tx: dict) -> None:
        res.check("raid: protocol", "saga", tx.get("protocol"))
        res.check("raid: status", "completed", tx.get("status"))
        res.check("raid: steps", {s: "executed" for s in STEPS}, self.steps(tx))
        time.sleep(SETTLE)
        after = Snapshot(self.b, players)
        for p in players:
            gained = after.balance[p.user_id] - before.balance[p.user_id]
            res.check(f"raid: {p.username}'s balance", "increased", f"{gained:+}", gained > 0)
            b, a = before.creature[p.primary], after.creature[p.primary]
            res.check(f"raid: {p.username}'s primary XP", "increased",
                      f"L{b['level']}/{b['xp']} -> L{a['level']}/{a['xp']}", (a["level"], a["xp"]) > (b["level"], b["xp"]))
        res.check("raid: settlementStatus", "settled", self.f.get_raid(raid_id, players[0].user_id).get("settlementStatus"))
        res.check("raid.monster_defeated announced", 1, self.announced("raid.monster_defeated", raid_id, 1))
        self.record_branches(res, tx)

    # -------------------------------------------------------------------------------- fixtures
    def forfeited(self) -> tuple[Player, Player, str, Snapshot, str]:
        """A decided battle (b forfeits); returns the players, battle, snapshot before and the saga's ID."""
        a, b, battle_id = self.duel()
        before = Snapshot(self.b, [a, b])
        return a, b, battle_id, before, self.forfeit_and_find(battle_id, a, b)["transactionId"]

    def killed_broker(self, res: Result) -> None:
        self.c.run("kill", "message-broker")
        res.note(f"Broker killed at {now()}")

    # ------------------------------------------------------------------------------- scenarios
    def s1(self, res: Result) -> None:
        a, b, battle_id = self.duel()
        before = Snapshot(self.b, [a, b])
        battle = self.f.knockout(battle_id, [a, b])
        winner, loser = (a, b) if battle.get("winnerId") == a.user_id else (b, a)
        tx = self.wait_saga(self.b.find_transaction("battle.settlement", battle_id)["transactionId"])
        res.note(f"battle {battle_id} to knockout, saga {tx['transactionId']}, winner {winner.username}")
        self.completed_battle(res, battle_id, winner, loser, before, tx)
        owner, member = self.f.player("o", "flame"), self.f.player("m", "electric")
        raid_id = self.f.raid(self.f.guild(owner, [member]), owner, [member], self.f.raid_definition())
        before = Snapshot(self.b, [owner, member])
        self.f.defeat(raid_id, [owner, member])
        tx = self.wait_saga(self.b.find_transaction("raid.payout", raid_id)["transactionId"])
        res.note(f"raid {raid_id}, saga {tx['transactionId']}")
        self.raid_completed(res, [owner, member], raid_id, before, tx)

    def s2(self, res: Result) -> None:
        loser = self.f.player("l", "nature")
        first, second = self.f.player("w", "flame"), self.f.player("x", "water")
        self.f.friends(first, loser)
        self.f.friends(second, loser)
        battle1, battle2 = self.f.battle(first, loser), self.f.battle(second, loser)
        tx1 = self.wait_saga(self.forfeit_and_find(battle1, first, loser)["transactionId"])
        res.check("first saga", "completed", tx1.get("status"))
        middle = Snapshot(self.b, [loser, first, second])
        tx2 = self.wait_saga(self.forfeit_and_find(battle2, second, loser)["transactionId"])
        res.note(f"battles {battle1} and {battle2}; sagas {tx1['transactionId']} and {tx2['transactionId']}")
        res.check("second saga status", "compensated", tx2.get("status"))
        res.check("reason", "TAMAGOTCHI_NOT_OWNED", tx2.get("reason"))
        steps = self.steps(tx2)
        res.check("currency and verify compensated", {"verify": "compensated", "currency": "compensated"},
                  {s: steps.get(s) for s in ("verify", "currency")})
        res.note(f"second saga steps: {json.dumps(steps)}")
        time.sleep(SETTLE)
        res.check("balances, XP and owners", "as after the first battle",
                  "as after the first battle" if Snapshot(self.b, [loser, first, second]).same(middle) else "changed", None)
        res.check("second settlementStatus", "failed", self.f.get_battle(battle2, second.user_id).get("settlementStatus"))
        res.check("battle.finished for the second battle", 0, self.announced("battle.finished", battle2, 0))
        res.check("Tamagotchi holds the second battle's key", "1", self.ledger_keys(battle2)["tamagotchi"])
        self.record_branches(res, tx2)

    def s3(self, res: Result) -> None:
        # A: raid.payout for a raid nobody attacked, through the Broker.
        players, raid_id = self.raid_party(defeat=False)
        raid = self.f.get_raid(raid_id, players[0].user_id)
        before = Snapshot(self.b, players)
        transaction_id = new_id()
        r = self.b.own("POST", "/api/v1/transactions", {
            "transactionId": transaction_id, "operation": "raid.payout", "operationVersion": 1,
            "businessKey": raid_id, "correlationId": new_id(),
            "payload": {"raidId": raid_id, "guildId": raid.get("guildId"), "monsterId": raid.get("monsterId"),
                        "totalDamage": 0, "durationSeconds": 1, "defeatedAt": now(),
                        "rewards": [{"userId": p.user_id, "tamagotchiId": p.primary, "globalCurrency": 100, "xp": 50,
                                     "damageShare": 0.5} for p in players]}})
        res.check("wrong raid request: answer", 202, r.status)
        tx = self.wait_saga(transaction_id)
        res.note(f"active raid {raid_id}, saga {transaction_id}: {json.dumps(self.steps(tx))}")
        res.check("wrong raid request: status", "compensated", tx.get("status"))
        res.check("wrong raid request: reason", "RAID_NOT_DEFEATED", tx.get("reason"))
        time.sleep(SETTLE)
        res.check("wrong raid request: balances and XP", "unchanged",
                  "unchanged" if Snapshot(self.b, players).same(before) else "changed", None)
        res.check("raid still active", "active", self.f.get_raid(raid_id, players[0].user_id).get("status"))
        # B: a real battle's key and payload under another ID, while its own saga is held after verify.
        self.c.broker_settings(self.b, TX_PAUSE_POINT="after-step:verify", TX_PAUSE_MS="30000")
        try:
            a, b, battle_id, before, real_id = self.forfeited()
            real = self.wait_step(real_id, "verify", {"executed"})
            r = self.b.own("POST", "/api/v1/transactions", {
                "transactionId": new_id(), "operation": "battle.settlement", "operationVersion": 1,
                "businessKey": battle_id, "correlationId": new_id(), "payload": self.stored_payload(real_id)})
            res.check("another ID through the Broker: answer", "200 with the real saga",
                      f"{r.status} {'with the real saga' if r.get('transactionId') == real_id else r.get('transactionId')}")
            other = new_id()
            r = self.step_call("execute", real, "verify", transaction_id=other)
            res.check("another ID at Battle's verify", "rejected SETTLEMENT_MISMATCH", f"{r.get('result')} {r.get('code')}")
            r = self.step_call("compensate", real, "verify", transaction_id=other, reason="SETTLEMENT_MISMATCH")
            res.check("its compensation at Battle", "200 compensated", f"{r.status} {r.get('status')}")
            res.check("real settlement meanwhile", "pending", self.f.get_battle(battle_id, a.user_id).get("settlementStatus"))
            tx = self.wait_saga(real_id)
        finally:
            self.c.restore_broker(self.b)
        res.note(f"battle {battle_id}, real saga {real_id}, wrong ID {other}")
        self.completed_battle(res, battle_id, a, b, before, tx)

    def s4(self, res: Result) -> None:
        self.c.broker_settings(self.b, TX_REJECT_STEP="complete")
        try:
            a, b, battle_id, before, transaction_id = self.forfeited()
            tx = self.wait_saga(transaction_id)
            owner, member = self.f.player("o", "flame"), self.f.player("m", "electric")
            raid_id = self.f.raid(self.f.guild(owner, [member]), owner, [member], self.f.raid_definition())
            raid_before = Snapshot(self.b, [owner, member])
            self.f.defeat(raid_id, [owner, member])
            raid_tx = self.wait_saga(self.b.find_transaction("raid.payout", raid_id)["transactionId"])
        finally:
            self.c.restore_broker(self.b)
        res.note(f"battle {battle_id}, saga {transaction_id}: {json.dumps(self.steps(tx))}")
        self.compensated_battle(res, battle_id, a, b, before, tx, "REHEARSAL")
        res.note(f"raid {raid_id}, saga {raid_tx['transactionId']}: {json.dumps(self.steps(raid_tx))}")
        res.check("raid: status", "compensated", raid_tx.get("status"))
        res.check("raid: reason", "REHEARSAL", raid_tx.get("reason"))
        time.sleep(SETTLE)
        res.check("raid: balances, XP and levels", "as before",
                  "as before" if Snapshot(self.b, [owner, member]).same(raid_before) else "changed", None)
        res.check("raid: settlementStatus", "failed", self.f.get_raid(raid_id, owner.user_id).get("settlementStatus"))
        res.check("raid.monster_defeated announced", 0, self.announced("raid.monster_defeated", raid_id, 0))

    def s5(self, res: Result) -> None:
        for step in ("currency", "creatures"):
            self.c.broker_settings(self.b, TX_PAUSE_POINT=f"after-step:{step}", TX_PAUSE_MS="120000")
            try:
                a, b, battle_id, before, transaction_id = self.forfeited()
                self.wait_step(transaction_id, step, {"executed"})
                res.note(f"[after {step}] held: {json.dumps(self.steps(self.b.transaction(transaction_id).body))}")
                self.killed_broker(res)
            finally:
                self.c.restore_broker(self.b)
            tx = self.wait_saga(transaction_id)
            res.note(f"[after {step}] battle {battle_id}, saga {transaction_id}")
            sub = Result(res.scenario, res.title)
            self.completed_battle(sub, battle_id, a, b, before, tx)
            for c in sub.checks:
                c.name = f"after {step}: {c.name}"
            res.checks += sub.checks
            res.notes += sub.notes

    def s6(self, res: Result) -> None:
        self.c.broker_settings(self.b, TX_REJECT_STEP="complete", TX_PAUSE_POINT="before-compensation:currency",
                               TX_PAUSE_MS="120000")
        try:
            a, b, battle_id, before, transaction_id = self.forfeited()
            held = self.wait_step(transaction_id, "creatures", {"compensated"})
            res.check("held mid-compensation", "compensating", held.get("status"))
            res.note(f"held before compensating currency: {json.dumps(self.steps(held))}")
            self.killed_broker(res)
        finally:
            self.c.restore_broker(self.b)
        tx = self.wait_saga(transaction_id)
        res.note(f"battle {battle_id}, saga {transaction_id}")
        self.compensated_battle(res, battle_id, a, b, before, tx, "REHEARSAL")
        rows = self.branches("tamagotchi", transaction_id)
        res.check("Tamagotchi branch rows", ["compensated"], [r["outcome"] for r in rows])

    def s7(self, res: Result) -> None:
        a, b, battle_id = self.duel()
        before = Snapshot(self.b, [a, b])
        self.c.run("stop", "tamagotchi")
        try:
            transaction_id = self.forfeit_and_find(battle_id, a, b)["transactionId"]
            self.wait_step(transaction_id, "currency", {"executed"})
            time.sleep(15)  # longer than the 2PC voting window of 10 s
            held = self.b.transaction(transaction_id).body
            res.check("status while Tamagotchi is away", "executing", held.get("status"))
            res.check("creatures step", "pending", self.steps(held).get("creatures"))
            res.note(f"creatures lastError: {next((s.get('lastError') for s in held['steps'] if s['step'] == 'creatures'), None)}")
            res.check("prepared transactions", {p: [] for p in DATABASES},
                      {p: v for p, v in self.in_doubt().items()})
            res.check("sessions idle in a transaction", {"user-management": "0", "battle": "0"}, self.idle_in_transaction())
        finally:
            self.c.run("start", "tamagotchi")
        tx = self.wait_saga(transaction_id, timeout=240)
        res.note(f"battle {battle_id}, saga {transaction_id}")
        self.completed_battle(res, battle_id, a, b, before, tx)

    def s8(self, res: Result) -> None:
        # Before the pivot: Tamagotchi away past a 15 s deadline.
        a, b, battle_id = self.duel()
        before = Snapshot(self.b, [a, b])
        self.c.broker_settings(self.b, TX_SAGA_DEADLINE_MS="15000")
        try:
            self.c.run("stop", "tamagotchi")
            try:
                transaction_id = self.forfeit_and_find(battle_id, a, b)["transactionId"]
                time.sleep(25)
                res.note(f"[before the pivot] after the deadline: {json.dumps(self.steps(self.b.transaction(transaction_id).body))}")
            finally:
                self.c.run("start", "tamagotchi")
            tx = self.wait_saga(transaction_id, timeout=240)
        finally:
            self.c.restore_broker(self.b)
        res.note(f"[before the pivot] battle {battle_id}, saga {transaction_id}")

        def deadline(reason: str) -> bool:
            """DEADLINE_EXCEEDED: creatures"""
            return reason.startswith("DEADLINE_EXCEEDED") and "creatures" in reason
        self.compensated_battle(res, battle_id, a, b, before, tx, deadline)
        # After the pivot: held after creatures for longer than a 5 s deadline.
        self.c.broker_settings(self.b, TX_SAGA_DEADLINE_MS="5000", TX_PAUSE_POINT="after-step:creatures",
                               TX_PAUSE_MS="15000")
        try:
            a, b, battle_id, before, transaction_id = self.forfeited()
            tx = self.wait_saga(transaction_id)
        finally:
            self.c.restore_broker(self.b)
        res.note(f"[after the pivot] battle {battle_id}, saga {transaction_id}")
        sub = Result(res.scenario, res.title)
        self.completed_battle(sub, battle_id, a, b, before, tx)
        for c in sub.checks:
            c.name = f"after the pivot: {c.name}"
        res.checks += sub.checks
        res.notes += sub.notes

    def s9(self, res: Result) -> None:
        writer = self.c.writer("tamagotchi")
        standbys = [n for n in DATABASES["tamagotchi"][0] if n != writer and self.c.query("tamagotchi",
                    "SELECT pg_is_in_recovery()", node=n) == "t"]
        if writer is None or len(standbys) != 2:
            raise StepFailed(f"Tamagotchi needs a writer and two standbys (writer {writer}, standbys {standbys})")
        nodes = [writer, *standbys]  # any writer: the scenario can run again after a rejoin
        self.c.broker_settings(self.b, TX_PAUSE_POINT="after-step:currency", TX_PAUSE_MS="90000")
        try:
            a, b, battle_id, before, transaction_id = self.forfeited()
            self.wait_step(transaction_id, "currency", {"executed"})
            self.c.run("kill", nodes[0])

            def replay_position(node: str) -> int:
                position = self.c.query("tamagotchi", "SELECT pg_last_wal_replay_lsn() - '0/0'::pg_lsn", node=node)
                if position is None:
                    raise StepFailed(f"could not read {node}'s replay position")
                return int(position)

            candidate, remaining = sorted(nodes[1:], key=replay_position, reverse=True)
            p = subprocess.run(["bash", str(ROOT / "scripts/ha/postgres-promote.sh"), nodes[0], candidate, remaining],
                               capture_output=True, text=True)
            if p.returncode:
                raise StepFailed(f"standby promotion failed: {(p.stdout + p.stderr).strip().splitlines()[-1:]}")
            res.note(f"{nodes[0]} killed between currency and creatures; {p.stdout.strip().splitlines()[-1]}")
            res.check("new Tamagotchi writer", candidate, self.c.writer("tamagotchi"))
            tx = self.wait_saga(transaction_id, timeout=240)
        finally:
            self.c.restore_broker(self.b)
        res.note(f"battle {battle_id}, saga {transaction_id}")
        self.completed_battle(res, battle_id, a, b, before, tx)
        rows = self.branches("tamagotchi", transaction_id)
        res.check(f"creatures branch on {candidate}", ["executed"], [r["outcome"] for r in rows])
        res.note(f"Topology changed: {nodes[0]} is fenced; rejoin it as in the README before the next run.")

    def s10(self, res: Result) -> None:
        a, b, battle_id, before, transaction_id = self.forfeited()
        tx = self.wait_saga(transaction_id)
        res.check("completed saga", "completed", tx.get("status"))
        time.sleep(SETTLE)
        done = Snapshot(self.b, [a, b])
        for step in STEPS:
            answers = [self.step_call("execute", tx, step) for _ in range(2)]
            res.check(f"execute {step} twice", "200 executed both times",
                      "200 executed both times" if all(r.status == 200 and r.get("result") in ("executed", "already_applied")
                                                       for r in answers) else [f"{r.status} {r.body}" for r in answers],
                      None)
        time.sleep(SETTLE)
        res.check("completed saga: balances, XP and owners", "unchanged",
                  "unchanged" if Snapshot(self.b, [a, b]).same(done) else "changed", None)
        res.check("completed saga: battle.finished announced", 1, self.announced("battle.finished", battle_id, 1))
        self.c.broker_settings(self.b, TX_REJECT_STEP="complete")
        try:
            c, d, battle2, before2, transaction2 = self.forfeited()
            tx2 = self.wait_saga(transaction2)
        finally:
            self.c.restore_broker(self.b)
        res.check("compensated saga", "compensated", tx2.get("status"))
        rows = {s: self.branches(STEP_SERVICE[tx2["operation"]][s], transaction2) for s in ("currency", "creatures")}
        for step in ("creatures", "currency", "verify"):
            answers = [self.step_call("compensate", tx2, step, reason="REHEARSAL") for _ in range(2)]
            res.check(f"compensate {step} twice", "200 compensated both times",
                      "200 compensated both times" if all(r.status == 200 and r.get("status") == "compensated"
                                                          for r in answers) else [f"{r.status} {r.body}" for r in answers],
                      None)
        time.sleep(SETTLE)
        res.check("compensated saga: balances, XP and owners", "as before",
                  "as before" if Snapshot(self.b, [c, d]).same(before2) else "changed", None)
        res.check("compensated saga: branch rows", "unchanged", "unchanged" if rows == {
            s: self.branches(STEP_SERVICE[tx2["operation"]][s], transaction2) for s in ("currency", "creatures")}
            else "changed", None)
        res.check("compensated saga: settlementStatus", "failed", self.f.get_battle(battle2, c.user_id).get("settlementStatus"))
        self.no_branch_in_doubt(res)

    def s11(self, res: Result) -> None:
        self.c.broker_settings(self.b, TX_REJECT_STEP="complete", TX_PAUSE_POINT="before-compensation:currency",
                               TX_PAUSE_MS="20000")
        try:
            a, b, battle_id, before, transaction_id = self.forfeited()
            self.wait_step(transaction_id, "creatures", {"compensated"})
            balance = self.b.service("GET", "user-management", f"/api/v1/internal/users/{a.user_id}/balances").get(
                "globalCurrency")
            r = self.b.service("POST", "user-management", f"/api/v1/internal/users/{a.user_id}/balances/adjust",
                               {"commandId": new_id(), "currency": "global", "packageId": None, "delta": -balance,
                                "reason": "Lab 5 saga scenario 11: the winner spends the reward"})
            res.check("winner spends the whole balance", f"200, {balance} spent", f"{r.status}, {balance} spent",
                      r.status == 200)
            tx = self.wait_saga(transaction_id)
        finally:
            self.c.restore_broker(self.b)
        res.note(f"battle {battle_id}, saga {transaction_id}: {json.dumps(self.steps(tx))}")
        res.check("status", "compensated", tx.get("status"))
        time.sleep(SETTLE)
        after = Snapshot(self.b, [a, b])
        res.check("winner's balance", 0, after.balance[a.user_id])
        res.check("loser's balance", before.balance[b.user_id], after.balance[b.user_id])
        compensation = self.c.query("user-management", "SELECT coalesce(compensation::text, 'null') FROM transaction_branches "
                                    f"WHERE transaction_id = '{transaction_id}' AND outcome = 'compensated'") or "null"
        res.check("shortfall recorded in User Management's compensation", "present", compensation,
                  "shortfall" in compensation.lower())
        res.check("creature back with the loser", b.user_id, after.creature[b.primary]["ownerId"])
        self.no_branch_in_doubt(res)

    def s12(self, res: Result) -> None:
        duels = [self.duel() for _ in range(3)]
        befores = [Snapshot(self.b, [a, b]) for a, b, _ in duels]
        (a1, b1, battle1), (a2, b2, battle2), (a3, b3, battle3) = duels
        self.c.broker_settings(self.b, TX_PROTOCOL="2pc", TX_PAUSE_POINT="before-decision", TX_PAUSE_MS="120000")
        try:
            id1 = self.forfeit_and_find(battle1, a1, b1)["transactionId"]
            self.wait_prepared(id1, _twopc.PARTICIPANTS["battle.settlement"])
            res.note(f"2PC transaction {id1} prepared everywhere, held before its decision")
            self.killed_broker(res)
            for battle_id, loser in ((battle2, b2), (battle3, b3)):  # straight to Battle: the Broker is down
                self.direct_forfeit(battle_id, loser)
            res.note("two battles decided while the Broker was down; their start requests wait in Battle's outbox")
            self.c.broker_settings(self.b, TX_PAUSE_POINT="after-step:verify", TX_PAUSE_MS="20000")
            id3 = self.b.find_transaction("battle.settlement", battle3, timeout=60)["transactionId"]
            self.wait_step(id3, "verify", {"executed"})
            r = self.b.own("POST", "/api/v1/topics/battle.finished/messages", {
                "eventId": new_id(), "eventType": "battle.finished", "eventVersion": 1, "occurredAt": now(),
                "producer": "battle-service", "correlationId": new_id(), "idempotencyKey": battle3,
                "payload": self.stored_payload(id3)})
            res.check("Lab 4 battle.finished queued for the third battle", 202, r.status)
            id2 = self.b.find_transaction("battle.settlement", battle2, timeout=60)["transactionId"]
            tx1 = self.b.wait_transaction(id1, {"committed", "rolled_back"}, 240)
            tx2, tx3 = self.wait_saga(id2, 240), self.wait_saga(id3, 240)
        finally:
            self.c.restore_broker(self.b)
        res.check("in-progress transaction", ("2pc", "committed"), (tx1.get("protocol"), tx1.get("status")))
        res.check("request from the outbox", ("saga", "completed"), (tx2.get("protocol"), tx2.get("status")))
        res.check("battle with the queued event", ("saga", "completed"), (tx3.get("protocol"), tx3.get("status")))
        res.check("its consumer steps", {"currency": "already_applied", "creatures": "already_applied"},
                  {s: self.steps(tx3).get(s) for s in ("currency", "creatures")})
        time.sleep(SETTLE)
        for n, ((a, b, battle_id), before) in enumerate(zip(duels, befores), 1):
            after = Snapshot(self.b, [a, b])
            gained = after.balance[a.user_id] - before.balance[a.user_id]
            res.check(f"battle {n}: winner paid", "increased", f"{gained:+}", gained > 0)
            res.check(f"battle {n}: creature transferred", a.user_id, after.creature[b.primary]["ownerId"])
            res.check(f"battle {n}: settlementStatus", "settled", self.f.get_battle(battle_id, a.user_id).get("settlementStatus"))
            expected = 2 if n == 3 else 1
            res.check(f"battle {n}: battle.finished announced" + (" (the Lab 4 event, then Battle's)" if n == 3 else ""),
                      expected, self.announced("battle.finished", battle_id, expected))
        payload = self.stored_payload(id3)
        after = Snapshot(self.b, [a3, b3])
        res.check("battle 3: winner paid the event's amount once", (payload.get("currency") or {}).get("winnerDelta"),
                  after.balance[a3.user_id] - befores[2].balance[a3.user_id])
        time.sleep(SETTLE)
        res.check("nothing changes afterwards", "same snapshot",
                  "same snapshot" if Snapshot(self.b, [a3, b3]).same(after) else "changed", None)
        self.no_branch_in_doubt(res)

    def direct_forfeit(self, battle_id: str, loser: Player) -> None:
        """POST /forfeit to Battle itself on the Compose network, as the Gateway would forward it."""
        network = subprocess.run(["docker", "inspect", "--format", "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}",
                                  self.c.containers("battle")[0]], capture_output=True, text=True).stdout.split()[0]
        headers = {"X-Gateway-Token": self.b.gateway_token, "X-Authenticated-User-Id": loser.user_id,
                   "X-Authenticated-Roles": "user", "X-Authenticated-Package-Id": self.b.package_id,
                   "X-Correlation-Id": new_id(), "Content-Type": "application/json"}
        cmd = ["docker", "run", "--rm", "--network", network, "curlimages/curl:8.11.1", "-s", "-o", "/dev/null",
               "-w", "%{http_code}", "-X", "POST", f"http://battle:8084/api/v1/battles/{battle_id}/forfeit"]
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.stdout.strip() != "200":
            raise StepFailed(f"direct forfeit of {battle_id}: {p.stdout.strip()} {p.stderr.strip()[-200:]}")

    def s13(self, res: Result) -> None:
        a, b = self.f.player("a", "flame"), self.f.player("b", "nature")
        before = Snapshot(self.b, [a, b])
        key = new_id()  # a battle nobody else settles
        payload = {"battleId": key, "winnerId": a.user_id, "loserId": b.user_id, "outcome": "forfeit", "turnCount": 1,
                   "transferredTamagotchiId": b.primary, "currency": {"winnerDelta": 250, "loserDelta": -100},
                   "xp": {"winner": {"primaryTamagotchiId": a.primary, "primaryXp": 30, "secondaryTamagotchiId": None,
                                     "secondaryXp": 0},
                          "loser": {"primaryTamagotchiId": b.primary, "primaryXp": 10, "secondaryTamagotchiId": None,
                                    "secondaryXp": 0}},
                   "finishedAt": now()}
        tx = {"transactionId": new_id(), "operation": "battle.settlement", "businessKey": key, "payload": payload}
        for step in ("currency", "creatures"):
            r = self.step_call("compensate", tx, step, reason="DEADLINE_EXCEEDED: currency")
            res.check(f"{step}: compensate first", "200 compensated", f"{r.status} {r.get('status')}")
            r = self.step_call("execute", tx, step)
            code = (r.body or {}).get("error", {}).get("code") if isinstance(r.body, dict) else None
            res.check(f"{step}: the late execute", "409 TRANSACTION_ABORTED", f"{r.status} {code}")
        time.sleep(SETTLE)
        res.check("balances, XP and owners", "unchanged", "unchanged" if Snapshot(self.b, [a, b]).same(before) else "changed",
                  None)
        res.check("business key recorded", {"user-management": "1", "tamagotchi": "1"}, self.ledger_keys(key))
        res.note(f"transaction {tx['transactionId']}, business key {key}, posted to the participants directly")

    def s14(self, res: Result) -> None:
        a, b, battle_id = self.duel()
        before = Snapshot(self.b, [a, b])
        started = datetime.now(timezone.utc)
        self.c.broker_settings(self.b, TX_SAGA_DEADLINE_MS="25000", TX_PAUSE_POINT="after-step:currency",
                               TX_PAUSE_MS="20000")
        try:
            transaction_id = self.forfeit_and_find(battle_id, a, b)["transactionId"]
            held = self.wait_step(transaction_id, "currency", {"executed"})
            r = self.step_call("execute", held, "creatures")
            res.check("creatures executed at Tamagotchi, answer kept from the Broker", "200 executed",
                      f"{r.status} {r.get('result')}")
            self.c.run("kill", "tamagotchi")
            res.note("Tamagotchi stopped: the Broker's own creatures request gets no answer")
            time.sleep(35)  # past the pause and the 25 s deadline
            res.note(f"after the deadline: {json.dumps(self.steps(self.b.transaction(transaction_id).body))}")
            self.c.run("start", "tamagotchi")
            tx = self.wait_saga(transaction_id, timeout=240)
            # Read before the restore: recreating the Broker discards this container's log.
            logs = self.c.run("logs", "--no-log-prefix", "--since", started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                              "message-broker", check=False)
        finally:
            self.c.run("start", "tamagotchi", check=False)
            self.c.restore_broker(self.b)
        res.note(f"battle {battle_id}, saga {transaction_id}")
        res.note("Broker log lines for this saga: " + " | ".join(
            line[-180:] for line in logs.splitlines() if transaction_id in line)[-1500:])
        lines = [line for line in logs.splitlines() if transaction_id in line and "resolv" in line.lower()]
        res.check("the Broker resolved creatures", "logged", lines[-1][-200:] if lines else "not logged", bool(lines))
        self.completed_battle(res, battle_id, a, b, before, tx)

    def s15(self, res: Result) -> None:
        a, b, battle_id = self.duel()  # accepted, still active: not decided
        before = Snapshot(self.b, [a, b])
        payload = self.battle_payload(battle_id, a, b, 250, -100, winner_xp=30, loser_xp=10)
        transaction_id = new_id()
        r = self.b.own("POST", "/api/v1/transactions", {
            "transactionId": transaction_id, "operation": "battle.settlement", "operationVersion": 1,
            "businessKey": battle_id, "correlationId": new_id(), "payload": payload})
        res.check("settlement for an undecided battle: answer", 202, r.status)
        tx = self.wait_saga(transaction_id)
        res.check("status", "compensated", tx.get("status"))
        res.check("reason", "BATTLE_NOT_DECIDED", tx.get("reason"))
        res.check("business key recorded", {"user-management": "1", "tamagotchi": "1"}, self.ledger_keys(battle_id))
        r = self.b.own("POST", "/api/v1/topics/battle.finished/messages", {
            "eventId": new_id(), "eventType": "battle.finished", "eventVersion": 1, "occurredAt": now(),
            "producer": "battle-service", "correlationId": new_id(), "idempotencyKey": battle_id, "payload": payload})
        res.check("Lab 4 battle.finished queued", 202, r.status)
        time.sleep(SETTLE * 2)
        res.check("balances, XP and owners", "unchanged", "unchanged" if Snapshot(self.b, [a, b]).same(before) else "changed",
                  None)
        res.check("ledger keys (the event was a duplicate)", {"user-management": "1", "tamagotchi": "1"},
                  self.ledger_keys(battle_id))
        res.note(f"battle {battle_id} (active), saga {transaction_id}: {json.dumps(self.steps(tx))}")
        self.no_branch_in_doubt(res)

    # ------------------------------------------------------------------------------- preflight
    def saga_preflight(self, raid: bool) -> bool:
        services = ["user-management", "tamagotchi", "battle"] + (["monster-raid"] if raid else [])
        rows = [("message-broker", "GET /api/v1/transactions", self.b.own("GET", "/api/v1/transactions?limit=1").status)]
        for service in services:
            r = self.b.service("POST", service, f"/api/v1/internal/transactions/{new_id()}/resolve", {})
            rows.append((service, "POST .../resolve (route present)", r.status))
        ok = True
        for name, what, status in rows:
            good = status == 200 if name == "message-broker" else status not in (0, 404, 401, 503)
            ok &= good
            print(f"  {'ok ' if good else 'NO '} {name:24} {what:40} {status}")
        return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", default="1,2,3,4,5,6,7,8,10,11,12,13,14,15", help="comma-separated, or 'all'")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--allow-promotion", action="store_true", help="allow scenario 9 to promote a standby")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--out", default=str(ROOT / "docs" / "lab5-saga-runs"))
    args = parser.parse_args()
    if args.list:
        for n, (title, expected) in SCENARIOS.items():
            print(f"{n:>2}. {title}\n    expected: {expected}")
        return 0
    numbers = list(SCENARIOS) if args.scenario == "all" else [int(n) for n in args.scenario.split(",")]
    run = SagaRun()
    print(f"== preflight for scenarios {','.join(map(str, numbers))}")
    ready = run.saga_preflight(raid=bool(NEEDS_RAID & set(numbers)))
    if args.preflight:
        return 0 if ready else 1
    if not ready and not args.force:
        print("The Saga routes are not served by every component these scenarios use; nothing was run.")
        return 1
    results = []
    for n in numbers:
        title, expected = SCENARIOS[n]
        res = Result(n, title)
        res.note(f"expected: {expected}")
        print(f"\n== {n}. {title}")
        if n == 9 and not args.allow_promotion:
            res.error = "skipped: promotes a Tamagotchi standby; run with --allow-promotion"
        else:
            try:
                getattr(run, f"s{n}")(res)
            except StepFailed as e:
                res.error = str(e)
            except Exception as e:  # noqa: BLE001 - record and continue
                res.error = f"{type(e).__name__}: {e}"
                traceback.print_exc()
        for c in res.checks:
            print(f"   {'ok  ' if c.ok else 'FAIL'} {c.name}: expected {c.expected}, observed {c.observed}")
        print(f"   => {'PASS' if res.passed else 'FAIL'}" + (f" ({res.error})" if res.error else ""))
        results.append(res)
    meta = {"title": "Lab 5 Saga scenarios (#197)", "command": "python scripts/tx/check-saga.py " + " ".join(sys.argv[1:]),
            "revisions": revisions(run.c)}
    path = write_report(results, meta, Path(args.out))
    print(f"\n{sum(r.passed for r in results)}/{len(results)} passed; recorded in {path}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
