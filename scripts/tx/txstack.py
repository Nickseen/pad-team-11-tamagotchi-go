"""Shared helpers for the Lab 5 transaction scenarios (#196 two-phase commit, later #197 Saga).

Talks to the running Compose stack the way the services and the Gateway do: every call goes through the
Message Broker published on 127.0.0.1:8091, client calls carry the Gateway token and a trusted user, and
service calls carry the service token. Databases are read with psql inside their containers. Only the
Python standard library is used.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BROKER = os.environ.get("BROKER_URL", "http://127.0.0.1:8091")

# Participant databases: Compose services of the writer and its standbys, user, database.
DATABASES = {
    "user-management": (["user-management-db", "user-management-db-1", "user-management-db-2"], "usermgmt", "usermgmt"),
    "tamagotchi": (["tamagotchi-db", "tamagotchi-db-1", "tamagotchi-db-2"], "tamagotchi", "tamagotchi"),
    "battle": (["battle-db", "battle-db-1", "battle-db-2"], "battle", "battle"),
    "monster-raid": (["monster-raid-db", "monster-raid-db-1", "monster-raid-db-2"], "raid", "raid"),
}
PARTICIPANTS = {
    "battle.settlement": ["user-management", "tamagotchi", "battle"],
    "raid.payout": ["user-management", "tamagotchi", "monster-raid"],
}
APP_SERVICES = ["message-broker", "user-management", "tamagotchi", "battle", "monster-raid-1", "monster-raid-2"]
FINAL = {"committed", "rolled_back"}


def new_id() -> str:
    return str(uuid.uuid4())


def now() -> str:
    t = datetime.now(timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def env_value(name: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{name} is not set in .env")


class StepFailed(Exception):
    """A setup step did not answer as expected; the scenario cannot be judged."""


# --------------------------------------------------------------------------------------------- HTTP
@dataclass
class Reply:
    status: int
    body: object

    def get(self, key, default=None):
        return self.body.get(key, default) if isinstance(self.body, dict) else default


class Broker:
    def __init__(self) -> None:
        self.service_token = env_value("SERVICE_TOKEN")
        self.gateway_token = env_value("GATEWAY_TOKEN")
        self.package_id = ""  # every scenario user is registered with the fixtures' package

    def _send(self, method: str, url: str, headers: dict, body=None, timeout=30) -> Reply:
        data = None if body is None else json.dumps(body).encode()
        headers = {"X-Correlation-Id": new_id(), **headers}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw, status = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            return Reply(0, {"error": str(e)})
        try:
            return Reply(status, json.loads(raw) if raw else None)
        except ValueError:
            return Reply(status, raw.decode(errors="replace"))

    def client(self, method: str, service: str, path: str, user: str | None = None, body=None,
               roles: str = "user") -> Reply:
        """A client request as the Gateway forwards it: Gateway token plus the trusted user context."""
        headers = {"X-Service-Token": self.service_token, "X-Gateway-Token": self.gateway_token}
        if user:
            headers.update({"X-Authenticated-User-Id": user, "X-Authenticated-Roles": roles})
            if self.package_id:  # the JWT's packageId, as the Gateway forwards it
                headers["X-Authenticated-Package-Id"] = self.package_id
        return self._send(method, f"{BROKER}/api/v1/queues/{service}{path}", headers, body)

    def service(self, method: str, service: str, path: str, body=None, headers=None) -> Reply:
        """A service-to-service request through the Broker's request queue."""
        return self._send(method, f"{BROKER}/api/v1/queues/{service}{path}",
                          {"X-Service-Token": self.service_token, **(headers or {})}, body)

    def own(self, method: str, path: str, body=None) -> Reply:
        """The Broker's own routes: transactions, topics."""
        return self._send(method, f"{BROKER}{path}", {"X-Service-Token": self.service_token}, body)

    def metric(self, name: str, **labels) -> float:
        try:
            with urllib.request.urlopen(f"{BROKER}/metrics", timeout=10) as r:
                text = r.read().decode()
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            return float("nan")
        total = 0.0
        for line in text.splitlines():
            if not line.startswith(name + "{") and not line.startswith(name + " "):
                continue
            if all(f'{k}="{v}"' in line for k, v in labels.items()):
                total += float(line.rsplit(" ", 1)[1])
        return total

    def ready(self, timeout=90) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if self.own("GET", "/api/v1/transactions?limit=1").status in (200, 404):
                return
            time.sleep(1)
        raise StepFailed("the Message Broker did not come back")

    # ---- transactions (contract: docs/lab5-transaction-contract.md)
    def transaction(self, transaction_id: str) -> Reply:
        return self.own("GET", f"/api/v1/transactions/{transaction_id}")

    def find_transaction(self, operation: str, business_key: str, timeout=30) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            r = self.own("GET", f"/api/v1/transactions?operation={operation}&businessKey={business_key}&limit=5")
            items = r.get("items") or []
            if r.status == 200 and items:
                return items[0]
            time.sleep(0.5)
        raise StepFailed(f"no {operation} transaction for {business_key} after {timeout}s")

    def wait_transaction(self, transaction_id: str, until=FINAL, timeout=120) -> dict:
        end, last = time.time() + timeout, None
        while time.time() < end:
            r = self.transaction(transaction_id)
            if r.status == 200:
                last = r.body
                if last.get("status") in until:
                    return last
            time.sleep(0.5)
        raise StepFailed(f"transaction {transaction_id} did not reach {sorted(until)}: last {json.dumps(last)}")

    def prepared(self, service: str) -> list[str] | None:
        r = self.service("GET", service, "/api/v1/internal/transactions/prepared")
        if r.status != 200:
            return None
        return [i.get("transactionId") for i in r.get("items") or []]


# ------------------------------------------------------------------------------------------ Compose
class Compose:
    def __init__(self) -> None:
        self.files = ["-f", str(ROOT / "docker-compose.yml")] + shlex.split(os.environ.get("COMPOSE_FILES", ""))
        self.override: Path | None = None

    def run(self, *args: str, check=True, extra_files=()) -> str:
        cmd = ["docker", "compose", "--project-directory", str(ROOT), *self.files, *extra_files, *args]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if check and p.returncode:
            raise StepFailed(f"{' '.join(args)}: {p.stderr.strip()[-400:]}")
        return p.stdout

    def psql(self, service: str, user: str, db: str, sql: str) -> str | None:
        cmd = ["docker", "compose", "--project-directory", str(ROOT), *self.files, "exec", "-T", service,
               "psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-U", user, "-d", db, "-c", sql]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return p.stdout.strip() if p.returncode == 0 else None

    def writer(self, participant: str) -> str | None:
        nodes, user, db = DATABASES[participant]
        for node in nodes:
            if self.psql(node, user, db, "SELECT pg_is_in_recovery()") == "f":
                return node
        return None

    def query(self, participant: str, sql: str, node: str | None = None) -> str | None:
        _, user, db = DATABASES[participant]
        node = node or self.writer(participant)
        return None if node is None else self.psql(node, user, db, sql)

    def containers(self, service: str) -> list[str]:
        return [c for c in self.run("ps", "-q", service, check=False).split() if c]

    # ---- Broker settings for rehearsals (TX_PAUSE_POINT, TX_PAUSE_MS, TX_RETRY_BACKOFF_MS, ...)
    def broker_settings(self, broker: Broker, **settings: str) -> None:
        """Recreate the Broker with these TX_* settings; no settings restores the stack's own config."""
        extra: tuple[str, ...] = ()
        if settings:
            lines = ["services:", "  message-broker:", "    environment:"]
            lines += [f'      {k}: "{v}"' for k, v in settings.items()]
            fd, path = tempfile.mkstemp(prefix="tx-broker-", suffix=".yml")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            self.override = Path(path)
            extra = ("-f", path)
        self.run("up", "-d", "--no-deps", "message-broker", extra_files=extra)
        broker.ready()

    def restore_broker(self, broker: Broker) -> None:
        self.broker_settings(broker)
        if self.override:
            self.override.unlink(missing_ok=True)
            self.override = None


# ------------------------------------------------------------------------------------------ fixtures
@dataclass
class Player:
    user_id: str
    username: str
    primary: str = ""
    creatures: list[str] = field(default_factory=list)


class Fixtures:
    """Creates the data a scenario needs, through the same routes the clients use."""

    def __init__(self, broker: Broker) -> None:
        self.b = broker
        self.package_id = ""
        self.owner = ""

    def expect(self, reply: Reply, *ok: int, what: str) -> Reply:
        if reply.status not in ok:
            raise StepFailed(f"{what}: {reply.status} {json.dumps(reply.body)[:300]}")
        return reply

    def package(self) -> str:
        if self.package_id:
            return self.package_id
        tag = new_id()[:8]
        self.owner = self.register(f"txo_{tag}", package=None).user_id
        r = self.expect(self.b.client("POST", "package-registry", "/api/v1/packages", self.owner,
                                      {"slug": f"tx-{tag}", "name": f"Transactions {tag}",
                                       "description": "Lab 5 transaction scenarios"}), 201, what="create package")
        self.package_id = self.b.package_id = r.get("packageId")
        self.expect(self.b.client("POST", "package-registry", f"/api/v1/packages/{self.package_id}/versions",
                                  self.owner, {"version": "1.0.0", "growthRules": {}, "statDefinitions": [
                                      {"key": "happiness", "label": "Happiness", "minValue": 0, "maxValue": 100,
                                       "higherIsBetter": True, "combatBonus": None}]}),
                    201, what="publish package version")
        return self.package_id

    def register(self, username: str, package: str | None = "") -> Player:
        package_id = self.package() if package == "" else (package or new_id())
        r = self.expect(self.b.client("POST", "user-management", "/api/v1/auth/register", None,
                                      {"username": username, "email": f"{username}@example.com",
                                       "password": "transactions-" + new_id()[:12], "packageId": package_id}),
                        201, what=f"register {username}")
        return Player(r.get("userId"), username)

    def player(self, name: str, combat_type="flame", creatures=1) -> Player:
        p = self.register(f"tx{name}_{new_id()[:6]}")
        for i in range(creatures):
            r = self.expect(self.b.client("POST", "tamagotchi", "/api/v1/tamagotchis", p.user_id,
                                          {"packageId": self.package(), "name": f"{name}{i}", "combatType": combat_type,
                                           "spriteRef": f"{name}.png", "stats": {"happiness": 60}}),
                            201, what=f"create a creature for {name}")
            p.creatures.append(r.get("tamagotchiId"))
        p.primary = p.creatures[0]
        self.expect(self.b.client("PUT", "tamagotchi", "/api/v1/users/me/primary-tamagotchi", p.user_id,
                                  {"tamagotchiId": p.primary}), 200, what=f"set {name}'s primary")
        return p

    def friends(self, a: Player, b: Player) -> None:
        r = self.expect(self.b.client("POST", "user-management", "/api/v1/users/me/friend-requests", a.user_id,
                                      {"targetUserId": b.user_id}), 201, what="friend request")
        self.expect(self.b.client("POST", "user-management",
                                  f"/api/v1/users/me/friend-requests/{r.get('requestId')}/accept", b.user_id),
                    200, what="accept friend request")

    def battle(self, challenger: Player, opponent: Player) -> str:
        r = self.expect(self.b.client("POST", "battle", "/api/v1/battles", challenger.user_id,
                                      {"opponentId": opponent.user_id, "primaryTamagotchiId": challenger.primary,
                                       "secondaryTamagotchiId": None, "boosts": []}), 201, what="challenge")
        battle_id = r.get("battleId")
        self.expect(self.b.client("POST", "battle", f"/api/v1/battles/{battle_id}/accept", opponent.user_id,
                                  {"primaryTamagotchiId": opponent.primary, "secondaryTamagotchiId": None,
                                   "boosts": []}), 200, what="accept")
        return battle_id

    def knockout(self, battle_id: str, players: list[Player]) -> dict:
        """Plays attacks in turn until the battle is finished; returns the finished Battle."""
        for _ in range(400):
            battle = self.get_battle(battle_id, players[0].user_id)
            if battle.get("status") != "active":
                return battle
            actor = battle.get("currentTurnUserId")
            r = self.b.client("POST", "battle", f"/api/v1/battles/{battle_id}/turns", actor,
                              {"commandId": new_id(), "action": "attack", "boostId": None})
            if r.status == 429:
                time.sleep(0.5)
            elif r.status != 200 and r.get("code") not in ("BATTLE_NOT_ACTIVE", "NOT_YOUR_TURN"):
                raise StepFailed(f"turn: {r.status} {json.dumps(r.body)[:300]}")
        raise StepFailed("the battle did not finish within 400 turns")

    def forfeit(self, battle_id: str, loser: Player) -> dict:
        return self.expect(self.b.client("POST", "battle", f"/api/v1/battles/{battle_id}/forfeit", loser.user_id),
                           200, what="forfeit").body

    def get_battle(self, battle_id: str, user: str) -> dict:
        return self.expect(self.b.client("GET", "battle", f"/api/v1/battles/{battle_id}", user), 200,
                           what="read battle").body

    # ---- raids
    def guild(self, owner: Player, members: list[Player]) -> str:
        tag = new_id().replace("-", "")[:4].upper()
        r = self.expect(self.b.client("POST", "guild", "/api/v1/guilds", owner.user_id,
                                      {"name": f"Transactions {tag}", "tag": tag, "description": "Lab 5"}),
                        201, what="create guild")
        guild_id = r.get("guildId")
        for m in members:
            inv = self.expect(self.b.client("POST", "guild", f"/api/v1/guilds/{guild_id}/invitations", owner.user_id,
                                            {"userId": m.user_id}), 201, what="invite")
            self.expect(self.b.client("POST", "guild", f"/api/v1/invitations/{inv.get('invitationId')}/accept",
                                      m.user_id), 200, what="accept invitation")
        return guild_id

    def raid_definition(self, max_hp=30, currency=100, xp=50) -> str:
        admin = self.owner or self.register(f"txa_{new_id()[:6]}").user_id
        m = self.expect(self.b.client("POST", "package-registry", "/api/v1/monsters", admin, {
            "name": f"Ledger beast {new_id()[:4]}", "description": "Lab 5 transaction scenarios",
            "spriteRefs": ["beast.png"], "maxHp": max_hp, "combatType": "water", "weaknesses": [],
            "resistances": [], "specialProperties": {}}, roles="admin,user"), 201, what="create monster")
        d = self.expect(self.b.client("POST", "package-registry", "/api/v1/raid-definitions", admin, {
            "monsterId": m.get("monsterId"), "durationSeconds": 900, "maxParticipants": 10, "minGuildLevel": 0,
            "rewardConfig": {"globalCurrency": currency, "xp": xp, "distribution": "equal"}, "scheduledAt": None},
            roles="admin,user"), 201, what="create raid definition")
        definition = d.get("raidDefinitionId")
        self.expect(self.b.client("POST", "package-registry", f"/api/v1/raid-definitions/{definition}/activate",
                                  admin, roles="admin,user"), 200, what="activate raid definition")
        return definition

    def raid(self, guild_id: str, owner: Player, members: list[Player], definition: str) -> str:
        r = None
        for _ in range(20):  # Monster Raid learns the activation from an event
            r = self.b.client("POST", "monster-raid", "/api/v1/raids", owner.user_id,
                              {"guildId": guild_id, "raidDefinitionId": definition})
            if r.status == 201:
                break
            time.sleep(1)
        raid_id = self.expect(r, 201, what="start raid").get("raidId")
        for p in [owner, *members]:
            self.expect(self.b.client("POST", "monster-raid", f"/api/v1/raids/{raid_id}/participants", p.user_id,
                                      {"primaryTamagotchiId": p.primary}), 201, what="join raid")
        return raid_id

    def defeat(self, raid_id: str, players: list[Player]) -> None:
        for i in range(600):
            p = players[i % len(players)]
            r = self.b.client("POST", "monster-raid", f"/api/v1/raids/{raid_id}/attacks", p.user_id,
                              {"commandId": new_id()})
            if r.status == 200 and r.get("raidStatus") == "defeated":
                return
            if r.status == 409:
                return
            if r.status == 429:
                time.sleep(0.3)
            elif r.status != 200:
                raise StepFailed(f"attack: {r.status} {json.dumps(r.body)[:300]}")
        raise StepFailed("the monster was not defeated within 600 attacks")

    def get_raid(self, raid_id: str, user: str) -> dict:
        return self.expect(self.b.client("GET", "monster-raid", f"/api/v1/raids/{raid_id}", user), 200,
                           what="read raid").body


# ----------------------------------------------------------------------------------------- snapshots
class Snapshot:
    """Balances, creatures and secondary references of the players involved, read by their owners."""

    def __init__(self, broker: Broker, players: list[Player]) -> None:
        self.balance: dict[str, int] = {}
        self.creature: dict[str, dict] = {}
        self.secondaries: dict[str, list[str]] = {}
        for p in players:
            r = broker.service("GET", "user-management", f"/api/v1/internal/users/{p.user_id}/balances")
            self.balance[p.user_id] = r.get("globalCurrency")
            r = broker.client("GET", "tamagotchi", f"/api/v1/users/{p.user_id}/secondary-tamagotchis", p.user_id)
            self.secondaries[p.user_id] = sorted(i.get("tamagotchiId") for i in r.get("items") or [])
        ids = sorted({c for p in players for c in p.creatures})
        r = broker.service("GET", "tamagotchi", f"/api/v1/internal/tamagotchis?ids={','.join(ids)}")
        for item in r.get("items") or []:
            self.creature[item["tamagotchiId"]] = {k: item.get(k) for k in
                                                   ("ownerId", "level", "xp", "capturedInBattleId")}

    def as_dict(self) -> dict:
        return {"balance": self.balance, "creature": self.creature, "secondaries": self.secondaries}

    def same(self, other: "Snapshot") -> bool:
        return self.as_dict() == other.as_dict()


# -------------------------------------------------------------------------------------------- report
@dataclass
class Check:
    name: str
    expected: str
    observed: str
    ok: bool


@dataclass
class Result:
    scenario: int
    title: str
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    def check(self, name: str, expected, observed, ok: bool | None = None) -> bool:
        ok = (expected == observed) if ok is None else ok
        self.checks.append(Check(name, str(expected), str(observed), bool(ok)))
        return bool(ok)

    def note(self, text: str) -> None:
        self.notes.append(text)
        print(f"   {text}")

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.ok for c in self.checks)


def revisions(compose: Compose) -> dict:
    """The exact code under test: CPR commit, submodule commits and running images with their digests."""
    def git(*args):
        p = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
        return p.stdout.strip()

    images = {}
    for service in APP_SERVICES:
        for cid in compose.containers(service)[:1]:
            p = subprocess.run(["docker", "inspect", "--format",
                                '{{.Config.Image}}|{{.Image}}|{{index .Config.Labels "org.opencontainers.image.revision"}}',
                                cid], capture_output=True, text=True)
            image, image_id, revision = (p.stdout.strip().split("|") + ["", "", ""])[:3]
            images[service] = {"image": image, "id": image_id[:19], "revision": revision[:12] or None}
    broker_env = {}
    for cid in compose.containers("message-broker")[:1]:
        p = subprocess.run(["docker", "inspect", "--format", "{{json .Config.Env}}", cid], capture_output=True, text=True)
        broker_env = {k: v for k, v in (e.split("=", 1) for e in json.loads(p.stdout or "[]"))
                      if k.startswith("TX_")}
    return {"cpr": git("rev-parse", "--short=12", "HEAD"), "cprDirty": bool(git("status", "--porcelain", "--", ".", ":!services")),
            "submodules": git("submodule", "status").splitlines(), "images": images, "brokerTx": broker_env}


def write_report(results: list[Result], meta: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    data = {"recordedAt": now(), **meta, "results": [
        {"scenario": r.scenario, "title": r.title, "passed": r.passed, "error": r.error, "notes": r.notes,
         "checks": [c.__dict__ for c in r.checks]} for r in results]}
    (out_dir / f"{stamp}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    lines = [f"# {meta['title']}", "", f"Recorded {data['recordedAt']} with `{meta['command']}`.", "",
             f"CPR `{meta['revisions']['cpr']}`" + (" (uncommitted changes)" if meta["revisions"]["cprDirty"] else ""),
             "", "| Service | Image | Image ID | Revision label |", "| --- | --- | --- | --- |"]
    lines += [f"| {s} | `{i['image']}` | `{i['id']}` | `{i['revision']}` |" for s, i in meta["revisions"]["images"].items()]
    lines += ["", "Submodules:", "", "```", *meta["revisions"]["submodules"], "```", ""]
    for r in results:
        lines += [f"## {r.scenario}. {r.title}: {'PASS' if r.passed else 'FAIL'}", ""]
        if r.error:
            lines += [f"Stopped: {r.error}", ""]
        if r.checks:
            lines += ["| Check | Expected | Observed | |", "| --- | --- | --- | --- |"]
            lines += [f"| {c.name} | {c.expected} | {c.observed} | {'ok' if c.ok else '**differs**'} |" for c in r.checks]
            lines.append("")
        lines += [f"- {n}" for n in r.notes] + ([""] if r.notes else [])
    path = out_dir / f"{stamp}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
