"""StreamSync session relay.

A tiny WebSocket room broker: one host per room, many viewers. Everything
that passes through is session metadata - positions, timestamps and
non-invertible fingerprint hashes. No media content exists anywhere in
this protocol, so the relay stores and forwards nothing copyrighted.

Run:  python relay_server.py [--port 8765]
It's a single asyncio process; thousands of viewers per instance is fine
because traffic is a few small messages per second per room, and fan-out
never waits on any one viewer: a slow connection drops its own messages
instead of stalling the room.

A host whose connection drops keeps its room for --host-grace seconds
(viewers are told the host is away) and can reclaim it with the token it
was given at creation - also before the relay has noticed the old
connection is dead, which the token displaces. A host that means to
leave says "end".
"""

import argparse
import asyncio
import json
import re
import secrets
import string

import websockets

MAX_MSG = 512 * 1024          # largest single frame (film fingerprint chunk)
MAX_VIEWERS = 5000
MAX_ROOMS = 10000
MAX_FP_CHUNKS = 256           # 256 x 16384 words is a 290-hour film
HOST_GRACE = 60.0
CODE_ALPHABET = string.ascii_uppercase + "23456789"  # no 0/O/1/I
_CHUNK_KEY = re.compile(r"fp_chunk_(\d+)$")

rooms = {}  # code -> Room
closing = set()  # close handshakes of displaced host sockets, in flight


class Room:
    def __init__(self, code, password, meta, host_ws):
        self.code = code
        self.password = password
        self.meta = meta
        self.host = host_ws
        self.token = secrets.token_urlsafe(18)
        self.viewers = set()
        self.cached = {}   # last state / film-fingerprint msgs for late joiners
        self.expiry = None


def make_code():
    while True:
        code = "-".join("".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
                        for _ in range(2))
        if code not in rooms:
            return code


async def send(ws, obj):
    if ws is None:
        return
    try:
        await ws.send(json.dumps(obj))
    except websockets.ConnectionClosed:
        pass


def broadcast(room, text):
    websockets.broadcast(room.viewers, text)


def cache_key(msg, t):
    """Which late-joiner cache slot a host message fills, or None. Only a
    fixed set of keys, so a host cannot grow the relay's memory at will."""
    if t in ("state", "fp_meta"):
        return t
    if t == "fp_chunk":
        m = _CHUNK_KEY.match(str(msg.get("ck", "")))
        if m and int(m.group(1)) < MAX_FP_CHUNKS:
            return m.group(0)
    return None


def end_room(room):
    if rooms.get(room.code) is room:
        del rooms[room.code]
    broadcast(room, json.dumps({"type": "ended"}))


async def expire(room, grace):
    await asyncio.sleep(grace)
    if room.host is None:
        end_room(room)


def drop(ws):
    """Close a displaced host socket without waiting for it. A dead peer
    never answers the close handshake, so awaiting it would hold up the
    new host's resume for the whole close timeout (10 s) - as long as the
    client waits for that reply before giving up on the attempt."""
    task = asyncio.ensure_future(ws.close())
    closing.add(task)                  # the loop only holds tasks weakly
    task.add_done_callback(closing.discard)


async def handle(ws, grace=HOST_GRACE):
    role, room = None, None
    try:
        async for raw in ws:
            if len(raw) > MAX_MSG:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            t = msg.get("type")
            if not isinstance(t, str):
                continue

            if t == "create" and role is None:
                if len(rooms) >= MAX_ROOMS:
                    await send(ws, {"type": "error", "reason": "relay full"})
                    continue
                meta = msg.get("meta")
                room = Room(make_code(), msg.get("password") or None,
                            meta if isinstance(meta, dict) else {}, ws)
                rooms[room.code] = room
                role = "host"
                await send(ws, {"type": "created", "code": room.code,
                                "token": room.token})

            elif t == "resume" and role is None:
                r = rooms.get(str(msg.get("code", "")).strip().upper())
                if r is None or not secrets.compare_digest(
                        str(msg.get("token", "")), r.token):
                    await send(ws, {"type": "error", "reason": "cannot resume"})
                else:
                    # A host socket still registered here is stale: the
                    # host has reconnected, typically after a blip left the
                    # old one half-open, which keepalive takes 40 s to
                    # notice. Refusing would end the session (the client
                    # treats any error as fatal), so the token displaces
                    # it. Rebind first, so the old handler's finally sees
                    # room.host is not its socket and leaves the room be.
                    old, room, role = r.host, r, "host"
                    room.host = ws
                    if room.expiry:
                        room.expiry.cancel()
                        room.expiry = None
                    if old is not None:
                        drop(old)
                    await send(ws, {"type": "resumed", "code": room.code})
                    broadcast(room, json.dumps({"type": "host_back"}))
                    await send(ws, {"type": "viewers", "n": len(room.viewers)})

            elif t == "join" and role is None:
                code = str(msg.get("code", "")).strip().upper()
                r = rooms.get(code)
                if r is None:
                    await send(ws, {"type": "error", "reason": "no such session"})
                elif r.password and msg.get("password") != r.password:
                    await send(ws, {"type": "error", "reason": "wrong password"})
                elif len(r.viewers) >= MAX_VIEWERS:
                    await send(ws, {"type": "error", "reason": "session full"})
                else:
                    room, role = r, "viewer"
                    room.viewers.add(ws)
                    await send(ws, {"type": "joined", "meta": room.meta,
                                    "viewers": len(room.viewers),
                                    "host_away": room.host is None})
                    for cached in list(room.cached.values()):
                        await ws.send(cached)
                    await send(room.host, {"type": "viewers",
                                           "n": len(room.viewers)})

            elif role == "host" and room is not None:
                if t == "end":
                    end_room(room)
                    room = None
                    break
                # host messages flow to every viewer; cache the ones a late
                # joiner needs (current state, film fingerprint chunks)
                key = cache_key(msg, t)
                if key:
                    room.cached[key] = raw
                broadcast(room, raw)

            elif role == "viewer" and room is not None:
                # viewer -> host only (verification results, hellos)
                if t in ("verified", "hello"):
                    await send(room.host, msg | {"viewers": len(room.viewers)})
    finally:
        if room is not None:
            if role == "host" and room.host is ws:
                room.host = None
                if rooms.get(room.code) is room:
                    broadcast(room, json.dumps({"type": "host_away"}))
                    room.expiry = asyncio.ensure_future(expire(room, grace))
            elif role == "viewer":
                room.viewers.discard(ws)
                await send(room.host, {"type": "viewers",
                                       "n": len(room.viewers)})


async def main(port, grace=HOST_GRACE):
    async def handler(ws):
        await handle(ws, grace)
    async with websockets.serve(handler, "0.0.0.0", port, max_size=MAX_MSG):
        print(f"StreamSync relay listening on :{port}")
        await asyncio.Future()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host-grace", type=float, default=HOST_GRACE,
                    help="seconds a disconnected host may take to resume")
    args = ap.parse_args()
    asyncio.run(main(args.port, args.host_grace))
