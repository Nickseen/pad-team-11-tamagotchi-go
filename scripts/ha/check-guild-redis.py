#!/usr/bin/env python3
"""
Guild chat and single-use WebSocket tickets while Guild's Redis fails and returns (#193).

    python scripts/ha/check-guild-redis.py          # from the CPR root, with the stack up
    python scripts/ha/check-guild-redis.py --kill   # a crash instead of a clean stop

Needs Python 3.10+ with `websockets` (pip install websockets), the stack running from this
Compose file and SERVICE_TOKEN in .env. Everything goes through the Gateway as a client would:
it registers two users, creates a guild, opens two chat sockets with Gateway tickets, then
stops `guild-redis` (or kills it with --kill), records what chat, tickets, readiness and history do,
starts it again and records the recovery. It prints one line per observation and leaves the stack
running. Guild's Redis has no volume and no append-only file: a clean stop saves a snapshot inside
the container, a crash does not, which decides whether ticket claims survive.
"""
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import warnings

from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

warnings.filterwarnings("ignore", category=DeprecationWarning)
KILL = "--kill" in sys.argv[1:]

GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:8080")
BROKER = os.environ.get("BROKER_URL", "http://127.0.0.1:8091")
ROOT = pathlib.Path(__file__).resolve().parents[2]


def env_value(name):
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"{name} is not set in .env")


SERVICE_TOKEN = env_value("SERVICE_TOKEN")


def http(method, url, body=None, token=None, headers=None):
    request = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **(headers or {})})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, raw.decode(errors="replace")


def code(answer):
    status, body = answer
    detail = body.get("error", {}).get("code") if isinstance(body, dict) and "error" in body else None
    return f"{status}{' ' + detail if detail else ''}"


def user(prefix):
    name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    password = "redis-check-" + uuid.uuid4().hex
    status, body = http("POST", f"{GATEWAY}/user-management/api/v1/auth/register",
                        {"username": name, "email": f"{name}@example.com", "password": password,
                         "packageId": str(uuid.uuid4())})
    assert status == 201, (status, body)
    _, login = http("POST", f"{GATEWAY}/user-management/api/v1/auth/login", {"username": name, "password": password})
    return body["userId"], login["accessToken"]


def ticket(token, guild_id):
    return http("POST", f"{GATEWAY}/api/v1/gateway/websocket-tickets",
                {"channel": "guild.chat", "resourceId": guild_id}, token=token)


def open_socket(url):
    """Connects and reads the chat.history frame; returns (socket, None) or (None, why it was refused)."""
    try:
        ws = connect(url, open_timeout=5)
    except InvalidStatus as refused:
        body = refused.response.body.decode(errors="replace")
        try:
            body = json.loads(body)["error"]["code"]
        except (ValueError, KeyError, TypeError):
            pass
        return None, f"upgrade refused {refused.response.status_code} {body}"
    except OSError as error:
        return None, f"connection failed {type(error).__name__}"
    first = json.loads(ws.recv(timeout=5))
    assert first["type"] == "chat.history", first
    return ws, None


def send(ws, text):
    ws.send(json.dumps({"type": "chat.send", "clientMessageId": str(uuid.uuid4()), "body": text}))


def receive_message(ws, text, timeout=3.0):
    """True when a chat.message with this body arrives within timeout."""
    deadline = time.monotonic() + timeout
    try:
        while (left := deadline - time.monotonic()) > 0:
            frame = json.loads(ws.recv(timeout=left))
            if frame.get("type") == "chat.message" and frame.get("body") == text:
                return True
    except TimeoutError:
        return False
    except ConnectionClosed as closed:
        return f"socket closed ({closed.rcvd.code if closed.rcvd else 'no close frame'})"
    return False


def replica(url):
    port = url.split("://", 1)[1].split("/", 1)[0].rsplit(":", 1)[-1]
    return {"18087": "guild-1", "28087": "guild-2", "38087": "guild-3"}.get(port, port)


def guild_ready():
    return code(http("GET", f"{BROKER}/api/v1/queues/guild/api/v1/status", headers={"X-Service-Token": SERVICE_TOKEN}))


def compose(*args):
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True)


def wait_ready(seconds=60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if guild_ready().startswith("200"):
            return True
        time.sleep(1)
    return False


def say(label, value):
    print(f"{label:<58} {value}")


def main():
    owner_id, owner = user("redis_own")
    member_id, member = user("redis_mem")
    _, guild = http("POST", f"{GATEWAY}/guild/api/v1/guilds", {"name": f"Redis check {uuid.uuid4().hex[:6]}",
                                                                 "tag": uuid.uuid4().hex[:4].upper(), "description": "#193"},
                    token=owner)
    guild_id = guild["guildId"]
    _, invitation = http("POST", f"{GATEWAY}/guild/api/v1/guilds/{guild_id}/invitations", {"userId": member_id},
                         token=owner)
    http("POST", f"{GATEWAY}/guild/api/v1/invitations/{invitation['invitationId']}/accept", token=member)
    say("guild with owner and member", guild_id)

    print("== Redis up")
    owner_url = ticket(owner, guild_id)[1]["url"]
    member_url = ticket(member, guild_id)[1]["url"]
    a, _ = open_socket(owner_url)
    b, _ = open_socket(member_url)
    say("owner / member sockets on", f"{replica(owner_url)} / {replica(member_url)}")
    send(a, "before outage")
    say("member receives owner's message", receive_message(b, "before outage"))
    say("owner's ticket used a second time", open_socket(owner_url)[1])
    ticket_issued = time.monotonic()

    print(f"== guild-redis {'killed' if KILL else 'stopped'}")
    compose("kill" if KILL else "stop", "guild-redis")
    time.sleep(3)
    say("Guild readiness through the Broker", guild_ready())
    send(a, "during outage")
    say("member receives a message sent during the outage", receive_message(b, "during outage"))
    say("new ticket from the Gateway", code(ticket(owner, guild_id)))
    say("chat history over REST", code(http("GET", f"{GATEWAY}/guild/api/v1/guilds/{guild_id}/messages", token=owner)))
    say("guild details over REST", code(http("GET", f"{GATEWAY}/guild/api/v1/guilds/{guild_id}", token=owner)))

    print("== guild-redis started again")
    started = time.monotonic()
    compose("start", "guild-redis")
    say("Guild ready again after", f"{time.monotonic() - started:.0f} s" if wait_ready() else "not within 60 s")
    replay_age = time.monotonic() - ticket_issued
    replay = open_socket(owner_url)
    say(f"owner's first ticket reused ({replay_age:.0f} s after issue, lifetime 60 s)",
        replay[1] or "ACCEPTED: the claim was lost with Redis")
    if replay[0]:
        replay[0].close()
    send(a, "after recovery, old socket")
    say("old sockets: member receives owner's message", receive_message(b, "after recovery, old socket"))
    fresh_owner = ticket(owner, guild_id)
    fresh_member = ticket(member, guild_id)
    say("new tickets", f"{code(fresh_owner)} / {code(fresh_member)}")
    c, why_c = open_socket(fresh_owner[1]["url"])
    d, why_d = open_socket(fresh_member[1]["url"])
    say("new sockets on", f"{replica(fresh_owner[1]['url'])} / {replica(fresh_member[1]['url'])}"
        if c and d else f"{why_c} / {why_d}")
    if c and d:
        send(c, "after recovery, new socket")
        say("new sockets: member receives owner's message", receive_message(d, "after recovery, new socket"))
    _, history = http("GET", f"{GATEWAY}/guild/api/v1/guilds/{guild_id}/messages", token=owner)
    bodies = [m.get("body") for m in history.get("items", [])] if isinstance(history, dict) else history
    say("history after recovery", bodies)
    for ws in (a, b, c, d):
        if ws:
            ws.close()


if __name__ == "__main__":
    main()
