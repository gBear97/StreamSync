"""StreamSync session relay.

A tiny WebSocket room broker: one host per room, many viewers. Everything
that passes through is session metadata - positions, timestamps and
non-invertible fingerprint hashes. No media content exists anywhere in
this protocol, so the relay stores and forwards nothing copyrighted.

Run:  python relay_server.py [--port 8765]
It's a single asyncio process; thousands of viewers per instance is fine
because traffic is a few small messages per second per room.
"""

import argparse
import asyncio
import json
import secrets
import string

import websockets

MAX_MSG = 512 * 1024          # largest single frame (film fingerprint chunk)
MAX_VIEWERS = 5000
CODE_ALPHABET = string.ascii_uppercase + "23456789"  # no 0/O/1/I

rooms = {}  # code -> Room


class Room:
    def __init__(self, code, password, meta, host_ws):
        self.code = code
        self.password = password
        self.meta = meta
        self.host = host_ws
        self.viewers = set()
        self.cached = {}   # last state / film-fingerprint msgs for late joiners


def make_code():
    while True:
        code = "-".join("".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
                        for _ in range(2))
        if code not in rooms:
            return code


async def send(ws, obj):
    try:
        await ws.send(json.dumps(obj))
    except websockets.ConnectionClosed:
        pass


async def broadcast(room, text):
    dead = []
    for v in room.viewers:
        try:
            await v.send(text)
        except websockets.ConnectionClosed:
            dead.append(v)
    for v in dead:
        room.viewers.discard(v)


async def handle(ws):
    role, room = None, None
    try:
        async for raw in ws:
            if len(raw) > MAX_MSG:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            t = msg.get("type")

            if t == "create" and role is None:
                room = Room(make_code(), msg.get("password") or None,
                            msg.get("meta") or {}, ws)
                rooms[room.code] = room
                role = "host"
                await send(ws, {"type": "created", "code": room.code})

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
                                    "viewers": len(room.viewers)})
                    for cached in room.cached.values():
                        await ws.send(cached)
                    await send(room.host, {"type": "viewers",
                                           "n": len(room.viewers)})

            elif role == "host" and room is not None:
                # host messages flow to every viewer; cache the ones a late
                # joiner needs (current state, film fingerprint chunks)
                if t in ("state", "fp_meta") or t.startswith("fp_chunk"):
                    room.cached[msg.get("ck", t)] = raw
                await broadcast(room, raw)

            elif role == "viewer" and room is not None:
                # viewer -> host only (verification results, hellos)
                if t in ("verified", "hello"):
                    await send(room.host, msg | {"viewers": len(room.viewers)})
    finally:
        if room is not None:
            if role == "host":
                rooms.pop(room.code, None)
                await broadcast(room, json.dumps({"type": "ended"}))
            elif role == "viewer":
                room.viewers.discard(ws)
                await send(room.host, {"type": "viewers",
                                       "n": len(room.viewers)})


async def main(port):
    async with websockets.serve(handle, "0.0.0.0", port, max_size=MAX_MSG):
        print(f"StreamSync relay listening on :{port}")
        await asyncio.Future()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    asyncio.run(main(ap.parse_args().port))
