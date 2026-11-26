#!/usr/bin/env python3
"""Two-phase commit scenarios of #196 (docs/lab5-transaction-contract.md, "Scenarios to record").

Runs against the stack started from this checkout (docker compose up -d) with the Broker published on
127.0.0.1:8091. Each scenario creates its own users, creatures, battles and raids through the Broker,
triggers the transaction, and compares what it observes with the contract's expected result. Every run
records the CPR commit, the submodule commits and the images under test, so the result is a checkpoint.

  python scripts/tx/check-2pc.py --list                 # the scenarios and their expected results
  python scripts/tx/check-2pc.py --preflight            # which components serve 2PC yet
  python scripts/tx/check-2pc.py --setup-only           # play a battle and a raid without 2PC checks
  python scripts/tx/check-2pc.py                        # scenarios 1-8, 10, 11
  python scripts/tx/check-2pc.py --scenario 5,6         # some of them
  python scripts/tx/check-2pc.py --scenario 9 --allow-promotion   # promotes a Tamagotchi standby

Scenarios 5-9 and 11 restart the Message Broker with rehearsal settings (TX_PAUSE_POINT, TX_PAUSE_MS,
TX_RETRY_BACKOFF_MS) through a temporary Compose override and restore its own settings afterwards.
Scenarios 7, 8 and 11 stop service replicas and start them again. Scenario 9 changes the database
topology: afterwards tamagotchi-db is fenced and tamagotchi-db-1 is the writer (rejoin: README,
"Database replication and failover"). Extra Compose files: COMPOSE_FILES="-f override.yml".
Results go to docs/lab5-2pc-runs/<time>.md and .json (--out to change).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from txstack import (DATABASES, PARTICIPANTS, ROOT, Broker, Compose, Fixtures, Player, Result,  # noqa: E402
                     Snapshot, StepFailed, new_id, now, revisions, write_report)

SCENARIOS = {
    1: ("A battle to knockout",
        "committed; balances, owner, secondary reference and XP changed once; battle.finished published after that"),
    2: ("Two battles with the same primary, the second settles after the creature changed owner",
        "rolled_back, TAMAGOTCHI_NOT_OWNED; no balance, XP or owner changed; settlementStatus failed; no event"),
    3: ("A defeated raid", "committed; every reward paid once"),
    4: ("raid.payout for a raid that is still active (posted by hand)", "rolled_back, RAID_NOT_DEFEATED; nothing changed"),
    5: ("Broker killed at before-decision", "after restart attempt 1 is rolled back and attempt 2 commits"),
    6: ("Broker killed at after-decision", "after restart the commit continues; no branch stays in doubt"),
    7: ("A participant killed after its vote", "its other replica, or the restarted one, commits the branch"),
    8: ("A participant down for longer than the voting window", "waiting, no locks held; commits once it returns"),
    9: ("Primary lost and a standby promoted while a branch is in doubt",
        "the branch is resolved on the promoted node; /prepared is empty afterwards"),
    10: ("The start request and a commit sent again", "same transaction, nothing applied twice"),
    11: ("A battle.finished queued by a 4.x producer, then a settlement of the same battle",
         "applied once; the vote is already_applied"),
}
RAID_SCENARIOS = {3, 4}  # the others settle battles
SETTLE = 5  # seconds to wait before checking that nothing changes any more


class Run:
    def __init__(self) -> None:
        self.b = Broker()
        self.c = Compose()
        self.f = Fixtures(self.b)

    # ------------------------------------------------------------------------------- observations
    def branches(self, participant: str, transaction_id: str) -> list[dict]:
        table = "raid.transaction_branches" if participant == "monster-raid" else "transaction_branches"
        out = self.c.query(participant,
                           "SELECT coalesce(json_agg(json_build_object('attempt', attempt, 'outcome', outcome, "
                           f"'effect', effect) ORDER BY attempt), '[]') FROM {table} "
                           f"WHERE transaction_id = '{transaction_id}'")
        if out is None:
            raise StepFailed(f"could not read {table} for {participant}")
        return json.loads(out)

    def in_doubt(self) -> dict[str, list[str]]:
        found = {}
        for participant in DATABASES:
            out = self.c.query(participant, "SELECT string_agg(gid, ',') FROM pg_prepared_xacts")
            found[participant] = [g for g in (out or "").split(",") if g] if out is not None else ["<unreachable>"]
        return found

    def events(self, topic: str) -> float:
        return self.b.metric("broker_events_published_total", topic=topic)

    def wait_prepared(self, transaction_id: str, services: list[str], timeout=40) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if all(transaction_id in (self.b.prepared(s) or []) for s in services):
                return
            time.sleep(0.5)
        raise StepFailed(f"not every participant prepared {transaction_id} within {timeout}s")

    def wait_status(self, transaction_id: str, statuses: set[str], timeout=60) -> dict:
        return self.b.wait_transaction(transaction_id, statuses, timeout)

    # ------------------------------------------------------------------------------------ checks
    def no_branch_in_doubt(self, res: Result) -> None:
        doubt = {k: v for k, v in self.in_doubt().items() if v}
        res.check("pg_prepared_xacts in every participant database", "empty", doubt or "empty", not doubt)

    def settled_battle(self, res: Result, battle_id: str, winner: Player, loser: Player, before: Snapshot,
                       tx: dict, events_before: float, attempt: int | None = 1, events_expected: int = 1) -> None:
        """The expected result of a committed battle.settlement (scenario 1 and every recovery scenario)."""
        res.check("transaction status", "committed", tx.get("status"))
        if attempt is not None:
            res.check("attempt", attempt, tx.get("attempt"))
        res.check("participants confirmed", "all committed",
                  "all committed" if all(p.get("state") == "committed" for p in tx.get("participants", []))
                  else json.dumps(tx.get("participants")), None)
        time.sleep(SETTLE)
        after = Snapshot(self.b, [winner, loser])
        moved = loser.primary
        res.check("transferred creature's owner", winner.user_id, after.creature[moved]["ownerId"])
        res.check("capturedInBattleId", battle_id, after.creature[moved]["capturedInBattleId"])
        added = sorted(set(after.secondaries[winner.user_id]) - set(before.secondaries[winner.user_id]))
        res.check("winner's new secondary references", [moved], added)
        gained = after.balance[winner.user_id] - before.balance[winner.user_id]
        lost = after.balance[loser.user_id] - before.balance[loser.user_id]
        res.check("winner's balance", "increased", f"{gained:+}", gained > 0)
        res.check("loser's balance", "decreased or clamped at 0", f"{lost:+}",
                  lost < 0 or (lost == 0 and before.balance[loser.user_id] == 0))
        effect = next((b["effect"] for b in self.branches("user-management", tx["transactionId"])
                       if b["outcome"] == "committed"), None)
        deltas = {d.get("userId"): d.get("delta") for d in (effect or {}).get("balances", []) if isinstance(d, dict)}
        if deltas:
            res.check("balances equal User Management's recorded effect", deltas,
                      {u: after.balance[u] - before.balance[u] for u in deltas})
        for p in (winner, loser):
            b, a = before.creature[p.primary], after.creature[p.primary]
            res.check(f"XP of {p.username}'s primary", "increased", f"L{b['level']}/{b['xp']} -> L{a['level']}/{a['xp']}",
                      (a["level"], a["xp"]) > (b["level"], b["xp"]))
        res.check("settlementStatus", "settled", self.f.get_battle(battle_id, winner.user_id).get("settlementStatus"))
        res.check("battle.finished published", events_expected, int(self.events("battle.finished") - events_before))
        time.sleep(SETTLE)
        res.check("nothing changes afterwards", "same snapshot", "same snapshot"
                  if Snapshot(self.b, [winner, loser]).same(after) else "changed", None)
        for participant in PARTICIPANTS["battle.settlement"]:
            rows = self.branches(participant, tx["transactionId"])
            res.note(f"{participant} transaction_branches: {json.dumps(rows)}")
        self.no_branch_in_doubt(res)

    # ---------------------------------------------------------------------------------- fixtures
    def duel(self) -> tuple[Player, Player, str]:
        a, b = self.f.player("a", "flame"), self.f.player("b", "nature")
        self.f.friends(a, b)
        return a, b, self.f.battle(a, b)

    def forfeit_and_find(self, battle_id: str, winner: Player, loser: Player) -> dict:
        self.f.forfeit(battle_id, loser)
        return self.b.find_transaction("battle.settlement", battle_id)

    def battle_payload(self, battle_id: str, winner: Player, loser: Player, winner_delta: int, loser_delta: int,
                       winner_xp: int = 0, loser_xp: int = 0) -> dict:
        battle = self.f.get_battle(battle_id, winner.user_id)
        return {"battleId": battle_id, "winnerId": winner.user_id, "loserId": loser.user_id,
                "outcome": "forfeit", "turnCount": battle.get("turnNumber", 1),
                "transferredTamagotchiId": loser.primary,
                "currency": {"winnerDelta": winner_delta, "loserDelta": loser_delta},
                "xp": {"winner": {"primaryTamagotchiId": winner.primary, "primaryXp": winner_xp,
                                  "secondaryTamagotchiId": None, "secondaryXp": 0},
                       "loser": {"primaryTamagotchiId": loser.primary, "primaryXp": loser_xp,
                                 "secondaryTamagotchiId": None, "secondaryXp": 0}},
                "finishedAt": battle.get("finishedAt") or now()}

    def raid_party(self, defeat: bool) -> tuple[list[Player], str]:
        owner, member = self.f.player("o", "flame"), self.f.player("m", "electric")
        guild = self.f.guild(owner, [member])
        raid_id = self.f.raid(guild, owner, [member], self.f.raid_definition())
        if defeat:
            self.f.defeat(raid_id, [owner, member])
        return [owner, member], raid_id

    # --------------------------------------------------------------------------------- scenarios
    def s1(self, res: Result) -> None:
        a, b, battle_id = self.duel()
        before, events = Snapshot(self.b, [a, b]), self.events("battle.finished")
        battle = self.f.knockout(battle_id, [a, b])
        res.check("battle outcome", "finished", battle.get("status"))
        winner, loser = (a, b) if battle.get("winnerId") == a.user_id else (b, a)
        tx = self.b.wait_transaction(self.b.find_transaction("battle.settlement", battle_id)["transactionId"])
        res.note(f"battle {battle_id}, transaction {tx['transactionId']}, winner {winner.username}")
        self.settled_battle(res, battle_id, winner, loser, before, tx, events)

    def s2(self, res: Result) -> None:
        loser = self.f.player("l", "nature")
        first, second = self.f.player("w", "flame"), self.f.player("x", "water")
        self.f.friends(first, loser)
        self.f.friends(second, loser)
        battle1, battle2 = self.f.battle(first, loser), self.f.battle(second, loser)
        events = self.events("battle.finished")
        tx1 = self.b.wait_transaction(self.forfeit_and_find(battle1, first, loser)["transactionId"])
        res.check("first battle", "committed", tx1.get("status"))
        middle = Snapshot(self.b, [loser, first, second])
        tx2 = self.b.wait_transaction(self.forfeit_and_find(battle2, second, loser)["transactionId"])
        res.note(f"battles {battle1} and {battle2}; transactions {tx1['transactionId']} and {tx2['transactionId']}")
        res.check("second transaction status", "rolled_back", tx2.get("status"))
        res.check("reason", "TAMAGOTCHI_NOT_OWNED", tx2.get("reason"))
        time.sleep(SETTLE)
        res.check("balances, XP and owners after the second settlement", "as after the first",
                  "as after the first" if Snapshot(self.b, [loser, first, second]).same(middle) else "changed", None)
        res.check("creature stays with the first winner", first.user_id,
                  Snapshot(self.b, [loser]).creature[loser.primary]["ownerId"])
        res.check("second settlementStatus", "failed", self.f.get_battle(battle2, second.user_id).get("settlementStatus"))
        res.check("battle.finished published", 1, int(self.events("battle.finished") - events))
        self.no_branch_in_doubt(res)

    def s3(self, res: Result) -> None:
        events = self.events("raid.monster_defeated")
        owner, member = self.f.player("o", "flame"), self.f.player("m", "electric")
        guild = self.f.guild(owner, [member])
        raid_id = self.f.raid(guild, owner, [member], self.f.raid_definition())
        before = Snapshot(self.b, [owner, member])
        self.f.defeat(raid_id, [owner, member])
        tx = self.b.wait_transaction(self.b.find_transaction("raid.payout", raid_id)["transactionId"])
        res.note(f"raid {raid_id}, transaction {tx['transactionId']}")
        res.check("transaction status", "committed", tx.get("status"))
        time.sleep(SETTLE)
        after = Snapshot(self.b, [owner, member])
        for p in (owner, member):
            gained = after.balance[p.user_id] - before.balance[p.user_id]
            res.check(f"{p.username}'s balance", "increased", f"{gained:+}", gained > 0)
            b, a = before.creature[p.primary], after.creature[p.primary]
            res.check(f"{p.username}'s primary XP", "increased", f"L{b['level']}/{b['xp']} -> L{a['level']}/{a['xp']}",
                      (a["level"], a["xp"]) > (b["level"], b["xp"]))
        res.check("settlementStatus", "settled", self.f.get_raid(raid_id, owner.user_id).get("settlementStatus"))
        res.check("raid.monster_defeated published", 1, int(self.events("raid.monster_defeated") - events))
        time.sleep(SETTLE)
        res.check("nothing changes afterwards", "same snapshot",
                  "same snapshot" if Snapshot(self.b, [owner, member]).same(after) else "changed", None)
        for participant in PARTICIPANTS["raid.payout"]:
            branches = self.branches(participant, tx["transactionId"])
            res.note(f"{participant} transaction_branches: {json.dumps(branches)}")
            res.check(f"{participant} branch", ["committed"], [branch["outcome"] for branch in branches])
        self.no_branch_in_doubt(res)

    def s4(self, res: Result) -> None:
        players, raid_id = self.raid_party(defeat=False)
        owner = players[0]
        raid = self.f.get_raid(raid_id, owner.user_id)
        before = Snapshot(self.b, players)
        transaction_id = new_id()
        r = self.b.own("POST", "/api/v1/transactions", {
            "transactionId": transaction_id, "operation": "raid.payout", "operationVersion": 1,
            "businessKey": raid_id, "correlationId": new_id(),
            "payload": {"raidId": raid_id, "guildId": raid.get("guildId"), "monsterId": raid.get("monsterId"),
                        "totalDamage": 0, "durationSeconds": 1, "defeatedAt": now(),
                        "rewards": [{"userId": p.user_id, "tamagotchiId": p.primary, "globalCurrency": 100, "xp": 50,
                                     "damageShare": 0.5} for p in players]}})
        res.check("start request answer", 202, r.status)
        tx = self.b.wait_transaction(transaction_id)
        res.note(f"raid {raid_id} (active), transaction {transaction_id}")
        res.check("transaction status", "rolled_back", tx.get("status"))
        res.check("reason", "RAID_NOT_DEFEATED", tx.get("reason"))
        time.sleep(SETTLE)
        res.check("balances and XP", "unchanged", "unchanged" if Snapshot(self.b, players).same(before) else "changed", None)
        res.check("raid status", "active", self.f.get_raid(raid_id, owner.user_id).get("status"))
        self.no_branch_in_doubt(res)

    def _killed_coordinator(self, res: Result, point: str, attempt: int) -> None:
        a, b, battle_id = self.duel()
        before, events = Snapshot(self.b, [a, b]), self.events("battle.finished")
        self.c.broker_settings(self.b, TX_PAUSE_POINT=point, TX_PAUSE_MS="120000")
        try:
            transaction_id = self.forfeit_and_find(battle_id, a, b)["transactionId"]
            if point == "before-decision":
                self.wait_prepared(transaction_id, PARTICIPANTS["battle.settlement"])
            else:
                self.wait_status(transaction_id, {"committing"})
                self.wait_prepared(transaction_id, PARTICIPANTS["battle.settlement"])
            held = self.b.transaction(transaction_id).body
            res.note(f"paused at {point}: status {held.get('status')}, attempt {held.get('attempt')}, "
                     f"decision {held.get('decision')}; branches prepared in all three participants")
            self.c.run("kill", "message-broker")
            res.note(f"Broker killed at {now()}")
        finally:
            self.c.restore_broker(self.b)
        tx = self.b.wait_transaction(transaction_id)
        res.note(f"battle {battle_id}, transaction {transaction_id}")
        self.settled_battle(res, battle_id, a, b, before, tx, events, attempt=attempt)
        if attempt == 2:
            res.check("attempt 1 in User Management", "rolled_back",
                      next((r["outcome"] for r in self.branches("user-management", transaction_id) if r["attempt"] == 1),
                           None))

    def s5(self, res: Result) -> None:
        self._killed_coordinator(res, "before-decision", attempt=2)

    def s6(self, res: Result) -> None:
        self._killed_coordinator(res, "after-decision", attempt=1)

    def s7(self, res: Result) -> None:
        a, b, battle_id = self.duel()
        before, events = Snapshot(self.b, [a, b]), self.events("battle.finished")
        replicas = self.c.containers("tamagotchi")
        if len(replicas) < 2:
            raise StepFailed("scenario 7 needs two Tamagotchi replicas")
        self.c.broker_settings(self.b, TX_PAUSE_POINT="before-decision", TX_PAUSE_MS="20000")
        try:
            transaction_id = self.forfeit_and_find(battle_id, a, b)["transactionId"]
            self.wait_prepared(transaction_id, PARTICIPANTS["battle.settlement"])
            subprocess.run(["docker", "kill", replicas[0]], capture_output=True)
            res.note(f"Tamagotchi replica {replicas[0][:12]} killed after its vote")
            tx = self.b.wait_transaction(transaction_id, timeout=180)
        finally:
            subprocess.run(["docker", "start", replicas[0]], capture_output=True)
            self.c.restore_broker(self.b)
        res.note(f"battle {battle_id}, transaction {transaction_id}")
        self.settled_battle(res, battle_id, a, b, before, tx, events)

    def s8(self, res: Result) -> None:
        a, b, battle_id = self.duel()
        before, events = Snapshot(self.b, [a, b]), self.events("battle.finished")
        self.c.run("stop", "tamagotchi")
        try:
            transaction_id = self.forfeit_and_find(battle_id, a, b)["transactionId"]
            held = self.wait_status(transaction_id, {"waiting"}, timeout=60)
            res.check("status while Tamagotchi is down", "waiting", held.get("status"))
            res.check("reason", "VOTE_MISSING: tamagotchi", held.get("reason"),
                      "VOTE_MISSING" in str(held.get("reason")) and "tamagotchi" in str(held.get("reason")))
            locks = {p: self.c.query(p, "SELECT count(*) FROM pg_prepared_xacts") for p in ("user-management", "battle")}
            res.check("prepared branches held by the others", {"user-management": "0", "battle": "0"}, locks)
            res.note(f"Tamagotchi down for {SETTLE * 3}s more than the voting window")
            time.sleep(SETTLE * 3)
        finally:
            self.c.run("start", "tamagotchi")
        tx = self.b.wait_transaction(transaction_id, timeout=240)
        res.note(f"battle {battle_id}, transaction {transaction_id}, committed at attempt {tx.get('attempt')}")
        self.settled_battle(res, battle_id, a, b, before, tx, events, attempt=None)
        res.check("attempt", ">= 2", tx.get("attempt"), (tx.get("attempt") or 0) >= 2)

    def s9(self, res: Result) -> None:
        nodes = DATABASES["tamagotchi"][0]
        if self.c.writer("tamagotchi") != nodes[0]:
            raise StepFailed(f"{nodes[0]} is not the Tamagotchi writer; rejoin the topology first")
        a, b, battle_id = self.duel()
        before, events = Snapshot(self.b, [a, b]), self.events("battle.finished")
        self.c.broker_settings(self.b, TX_PAUSE_POINT="after-decision", TX_PAUSE_MS="90000")
        try:
            transaction_id = self.forfeit_and_find(battle_id, a, b)["transactionId"]
            self.wait_status(transaction_id, {"committing"})
            self.wait_prepared(transaction_id, PARTICIPANTS["battle.settlement"])
            gid = self.c.query("tamagotchi", "SELECT string_agg(gid, ',') FROM pg_prepared_xacts", node=nodes[1]) or ""
            res.check(f"branch in doubt replicated to {nodes[1]}", "present", "present" if transaction_id in gid else gid,
                      transaction_id in gid)
            self.c.run("kill", nodes[0])
            p = subprocess.run(["bash", str(ROOT / "scripts/ha/postgres-promote.sh"), *nodes],
                               capture_output=True, text=True)
            res.note(f"{nodes[0]} killed, promote: {(p.stdout + p.stderr).strip().splitlines()[-1:]}")
            res.check("new Tamagotchi writer", nodes[1], self.c.writer("tamagotchi"))
            gid = self.c.query("tamagotchi", "SELECT string_agg(gid, ',') FROM pg_prepared_xacts", node=nodes[1]) or ""
            res.check("branch in doubt on the promoted node", "present", "present" if transaction_id in gid else gid,
                      transaction_id in gid)
            tx = self.b.wait_transaction(transaction_id, timeout=240)
        finally:
            self.c.restore_broker(self.b)
        res.note(f"battle {battle_id}, transaction {transaction_id}")
        self.settled_battle(res, battle_id, a, b, before, tx, events)
        res.check("Tamagotchi /prepared", [], self.b.prepared("tamagotchi"))
        res.note("Topology changed: tamagotchi-db is fenced; rejoin it as in the README before the next run.")

    def s10(self, res: Result) -> None:
        a, b, battle_id = self.duel()
        before, events = Snapshot(self.b, [a, b]), self.events("battle.finished")
        tx = self.b.wait_transaction(self.forfeit_and_find(battle_id, a, b)["transactionId"])
        res.check("first settlement", "committed", tx.get("status"))
        time.sleep(SETTLE)
        settled = Snapshot(self.b, [a, b])
        payload = tx.get("payload") or self.battle_payload(  # the Broker's view carries the stored payload
            battle_id, a, b, settled.balance[a.user_id] - before.balance[a.user_id],
            settled.balance[b.user_id] - before.balance[b.user_id])
        start = {"transactionId": tx["transactionId"], "operation": "battle.settlement", "operationVersion": 1,
                 "businessKey": battle_id, "correlationId": new_id(), "payload": payload}
        r = self.b.own("POST", "/api/v1/transactions", start)
        res.check("start request again: answer", 200, r.status)
        res.check("start request again: transaction", tx["transactionId"], r.get("transactionId"))
        r = self.b.own("POST", "/api/v1/transactions", {**start, "transactionId": new_id()})
        res.check("new transactionId, same battle: answer", 200, r.status)
        res.check("new transactionId, same battle: transaction", tx["transactionId"], r.get("transactionId"))
        phase = {"transactionId": tx["transactionId"], "attempt": tx.get("attempt"), "operation": "battle.settlement",
                 "operationVersion": 1, "businessKey": battle_id, "payload": payload}
        for participant in PARTICIPANTS["battle.settlement"]:
            r = self.b.service("POST", participant, f"/api/v1/internal/transactions/{tx['transactionId']}/commit", phase,
                               headers={"X-Broker-Attempt": "99"})
            res.check(f"commit again to {participant}", "200 committed", f"{r.status} {r.get('status')}")
        time.sleep(SETTLE)
        res.check("balances, XP and owners", "as after the first settlement",
                  "as after the first settlement" if Snapshot(self.b, [a, b]).same(settled) else "changed", None)
        res.check("transactions for this battle", 1,
                  len(self.b.own("GET", f"/api/v1/transactions?operation=battle.settlement&businessKey={battle_id}")
                      .get("items") or []))
        res.check("battle.finished published", 1, int(self.events("battle.finished") - events))
        self.no_branch_in_doubt(res)

    def s11(self, res: Result) -> None:
        a, b, battle_id = self.duel()
        before, events = Snapshot(self.b, [a, b]), self.events("battle.finished")
        self.c.broker_settings(self.b, TX_RETRY_BACKOFF_MS="20000")
        try:
            self.c.run("stop", "user-management", "tamagotchi")
            try:
                transaction_id = self.forfeit_and_find(battle_id, a, b)["transactionId"]
                self.wait_status(transaction_id, {"waiting"}, timeout=60)
                payload = self.battle_payload(battle_id, a, b, 250, -100, winner_xp=30, loser_xp=10)
                r = self.b.own("POST", "/api/v1/topics/battle.finished/messages", {
                    "eventId": new_id(), "eventType": "battle.finished", "eventVersion": 1, "occurredAt": now(),
                    "producer": "battle-service", "correlationId": new_id(), "idempotencyKey": battle_id,
                    "payload": payload})
                res.check("legacy battle.finished queued", 202, r.status)
                res.note("published as Battle 4.x would, while User Management and Tamagotchi were down")
            finally:
                self.c.run("start", "user-management", "tamagotchi")
            tx = self.b.wait_transaction(transaction_id, timeout=240)
        finally:
            self.c.restore_broker(self.b)
        res.note(f"battle {battle_id}, transaction {transaction_id}")
        res.check("transaction status", "committed", tx.get("status"))
        states = {p.get("service"): p.get("state") for p in tx.get("participants", [])}
        res.check("participant states", {"user-management": "already_applied", "tamagotchi": "already_applied",
                                         "battle": "committed"}, states)
        time.sleep(SETTLE)
        after = Snapshot(self.b, [a, b])
        res.check("winner's balance", f"{before.balance[a.user_id] + 250} (event's +250 once)", after.balance[a.user_id],
                  after.balance[a.user_id] == before.balance[a.user_id] + 250)
        expected_loser = max(0, before.balance[b.user_id] - 100)
        res.check("loser's balance", f"{expected_loser} (event's -100 once)", after.balance[b.user_id],
                  after.balance[b.user_id] == expected_loser)
        res.check("transferred creature's owner", a.user_id, after.creature[b.primary]["ownerId"])
        res.check("winner's new secondary references", [b.primary],
                  sorted(set(after.secondaries[a.user_id]) - set(before.secondaries[a.user_id])))
        for participant in ("user-management", "tamagotchi"):
            rows = self.branches(participant, transaction_id)
            res.check(f"{participant} branch (already_applied prepares nothing)", "no committed branch",
                      json.dumps(rows) if any(r["outcome"] == "committed" for r in rows) else "no committed branch",
                      not any(r["outcome"] == "committed" for r in rows))
        res.check("settlementStatus", "settled", self.f.get_battle(battle_id, a.user_id).get("settlementStatus"))
        res.check("battle.finished published (by hand, then by Battle)", 2, int(self.events("battle.finished") - events))
        time.sleep(SETTLE)
        res.check("nothing changes afterwards", "same snapshot",
                  "same snapshot" if Snapshot(self.b, [a, b]).same(after) else "changed", None)
        self.no_branch_in_doubt(res)

    # ------------------------------------------------------------------------------- preflight
    def preflight(self, participants: list[str]) -> bool:
        """Checks the Broker and the given participants only, so battle scenarios can run before Raid serves 2PC."""
        rows = [("message-broker", "GET /api/v1/transactions", self.b.own("GET", "/api/v1/transactions?limit=1").status)]
        for service in participants:
            rows.append((service, "GET /api/v1/internal/transactions/prepared",
                         self.b.service("GET", service, "/api/v1/internal/transactions/prepared").status))
        for participant in participants:
            setting = self.c.query(participant, "SHOW max_prepared_transactions")
            rows.append((f"{participant} database", "max_prepared_transactions", setting))
        ok = True
        for name, what, value in rows:
            good = value == 200 or (isinstance(value, str) and value.isdigit() and int(value) > 0)
            ok &= good
            print(f"  {'ok ' if good else 'NO '} {name:24} {what:45} {value}")
        return ok

    def setup_only(self) -> None:
        print("== battle to knockout (no 2PC checks)")
        a, b, battle_id = self.duel()
        before = Snapshot(self.b, [a, b])
        battle = self.f.knockout(battle_id, [a, b])
        print(f"   battle {battle_id}: {battle.get('status')}, {battle.get('turnNumber')} turns, winner "
              f"{'a' if battle.get('winnerId') == a.user_id else 'b'}, settlementStatus {battle.get('settlementStatus')}")
        time.sleep(SETTLE)
        after = Snapshot(self.b, [a, b])
        print(f"   balances {before.balance} -> {after.balance}")
        print(f"   creatures {before.creature} -> {after.creature}")
        print("== defeated raid (no 2PC checks)")
        players, raid_id = self.raid_party(defeat=True)
        raid = self.f.get_raid(raid_id, players[0].user_id)
        print(f"   raid {raid_id}: {raid.get('status')}, settlementStatus {raid.get('settlementStatus')}")
        print("== participant databases")
        for participant in DATABASES:
            print(f"   {participant}: writer {self.c.writer(participant)}, in doubt "
                  f"{self.c.query(participant, 'SELECT count(*) FROM pg_prepared_xacts')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", default="1,2,3,4,5,6,7,8,10,11", help="comma-separated numbers, or 'all'")
    parser.add_argument("--list", action="store_true", help="print the scenarios and their expected results")
    parser.add_argument("--preflight", action="store_true", help="check which components serve 2PC")
    parser.add_argument("--setup-only", action="store_true", help="play a battle and a raid without 2PC checks")
    parser.add_argument("--allow-promotion", action="store_true", help="allow scenario 9 to promote a standby")
    parser.add_argument("--force", action="store_true", help="run the scenarios even if the preflight fails")
    parser.add_argument("--out", default=str(ROOT / "docs" / "lab5-2pc-runs"))
    args = parser.parse_args()

    if args.list:
        for n, (title, expected) in SCENARIOS.items():
            print(f"{n:>2}. {title}\n    expected: {expected}")
        return 0
    run = Run()
    if args.setup_only:
        run.setup_only()
        return 0
    numbers = list(SCENARIOS) if args.scenario == "all" else [int(n) for n in args.scenario.split(",")]
    operations = {"raid.payout" if n in RAID_SCENARIOS else "battle.settlement" for n in numbers}
    needed = sorted({p for op in operations for p in PARTICIPANTS[op]}, key=list(DATABASES).index)
    print(f"== preflight for scenarios {','.join(map(str, numbers))}")
    ready = run.preflight(needed)
    if args.preflight:
        return 0 if ready else 1
    if not ready and not args.force:
        print("2PC is not served by every component these scenarios use; nothing was run (--force to run anyway).")
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
            except Exception as e:  # noqa: BLE001 - record and continue with the next scenario
                res.error = f"{type(e).__name__}: {e}"
                traceback.print_exc()
        for c in res.checks:
            print(f"   {'ok  ' if c.ok else 'FAIL'} {c.name}: expected {c.expected}, observed {c.observed}")
        print(f"   => {'PASS' if res.passed else 'FAIL'}" + (f" ({res.error})" if res.error else ""))
        results.append(res)

    meta = {"title": "Lab 5 two-phase commit scenarios (#196)",
            "command": "python scripts/tx/check-2pc.py " + " ".join(sys.argv[1:]),
            "revisions": revisions(run.c)}
    path = write_report(results, meta, Path(args.out))
    print(f"\n{sum(r.passed for r in results)}/{len(results)} passed; recorded in {path}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
