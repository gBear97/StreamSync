"""End-to-end relay test on localhost: rooms, passwords, caching for late
joiners, host->viewer broadcast, viewer->host routing, malformed input,
a host reconnecting with its token (also over its own stale socket), and
both ways a session ends."""

import base64
import json
import os
import socket
import subprocess
import sys
import time

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as _connect

PORT = 8899
URL = f"ws://127.0.0.1:{PORT}"


def connect(url):
    return _connect(url).__enter__()   # as session.Link does


def send(ws, obj):
    ws.send(json.dumps(obj))


def recv(ws, timeout=5):
    return json.loads(ws.recv(timeout=timeout))


def quiet_link(obj):
    """A connection that goes quiet the way a half-open one does: it opens,
    sends `obj`, and never reads again - so it never answers the relay's
    close handshake. Raw socket, because a websockets client answers it."""
    s = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n").encode())
    reply = b""
    while b"\r\n\r\n" not in reply:
        reply += s.recv(1024)
    assert b" 101 " in reply.split(b"\r\n")[0], reply
    data = json.dumps(obj).encode()
    assert len(data) < 126                   # one-byte length, masked text
    mask = os.urandom(4)
    s.sendall(bytes([0x81, 0x80 | len(data)]) + mask
              + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
    return s


def main():
    relay = subprocess.Popen([sys.executable, "relay_server.py",
                              "--port", str(PORT), "--host-grace", "2"])
    try:
        time.sleep(1.5)

        host = connect(URL)
        send(host, {"type": "create", "password": "swordfish",
                    "meta": {"title": "test film", "duration": 5400}})
        created = recv(host)
        assert created["type"] == "created", created
        code, token = created["code"], created["token"]
        print(f"room created: {code}")

        # host publishes fingerprint + first state BEFORE anyone joins
        send(host, {"type": "fp_meta", "ck": "fp_meta", "chunks": 1,
                    "words": 4, "duration": 5400, "title": "test film"})
        send(host, {"type": "fp_chunk", "ck": "fp_chunk_0", "i": 0,
                    "data": "AAABAAIAAwA="})
        send(host, {"type": "state", "ck": "state", "seq": 0, "pos": 100.0,
                    "utc": 123.0, "playing": True, "default_delay": 9})

        # wrong password rejected
        v_bad = connect(URL)
        send(v_bad, {"type": "join", "code": code, "password": "nope"})
        assert recv(v_bad)["reason"] == "wrong password"
        v_bad.close()
        print("wrong password: rejected")

        # unknown room rejected
        v_lost = connect(URL)
        send(v_lost, {"type": "join", "code": "XXXX-XXXX", "password": None})
        assert recv(v_lost)["reason"] == "no such session"
        v_lost.close()
        print("unknown room: rejected")

        # late joiner gets the cached fingerprint and state immediately
        viewer = connect(URL)
        send(viewer, {"type": "join", "code": code, "password": "swordfish"})
        got = [recv(viewer) for _ in range(4)]
        kinds = sorted(m["type"] for m in got)
        assert kinds == ["fp_chunk", "fp_meta", "joined", "state"], kinds
        print("late joiner: received cached fp_meta, fp_chunk and state")
        assert recv(host)["type"] == "viewers"

        # live broadcast reaches the viewer
        send(host, {"type": "state", "ck": "state", "seq": 1, "pos": 130.0,
                    "utc": 153.0, "playing": False, "default_delay": 9})
        live = recv(viewer)
        assert live["seq"] == 1 and live["playing"] is False
        send(host, {"type": "voice", "t0": 150.0, "data": "AAAA"})
        assert recv(viewer)["type"] == "voice"
        print("live broadcast: state + voice reach the viewer")

        # viewer -> host routing
        send(viewer, {"type": "verified", "ok": True, "offset": 0.25})
        routed = recv(host)
        assert routed["type"] == "verified" and routed["ok"] is True
        print("viewer->host routing: verified message delivered")

        # malformed input is ignored, not fatal to the host's handler
        host.send("[1, 2, 3]")
        send(host, {"no": "type"})
        send(host, {"type": 7})
        send(host, {"type": "fp_chunk", "ck": "anything-at-all", "i": 0,
                    "data": ""})
        send(host, {"type": "state", "ck": "state", "seq": 2, "pos": 131.0,
                    "utc": 154.0, "playing": True, "default_delay": 9})
        got = recv(viewer)
        while got["type"] != "state":        # the junk chunk still relays
            got = recv(viewer)
        assert got["seq"] == 2
        print("malformed messages: ignored; the room keeps working")

        # a dropped host is 'away', not gone, and can resume with its token
        host.close()
        assert recv(viewer)["type"] == "host_away"
        thief = connect(URL)
        send(thief, {"type": "resume", "code": code, "token": "guess"})
        assert recv(thief)["type"] == "error"
        thief.close()
        host = connect(URL)
        send(host, {"type": "resume", "code": code, "token": token})
        assert recv(host)["type"] == "resumed"
        assert recv(viewer)["type"] == "host_back"
        assert recv(host) == {"type": "viewers", "n": 1}
        send(host, {"type": "state", "ck": "state", "seq": 3, "pos": 140.0,
                    "utc": 163.0, "playing": True, "default_delay": 9})
        assert recv(viewer)["seq"] == 3
        print("host reconnect: away -> resumed with token (not without)")

        # a host can reconnect while the relay still holds its old socket -
        # half-open after a network blip, which keepalive takes 40 s to
        # notice. The session ends on any refusal, so the token displaces
        # that socket; a wrong token is still refused.
        thief = connect(URL)
        send(thief, {"type": "resume", "code": code, "token": "guess"})
        assert recv(thief) == {"type": "error", "reason": "cannot resume"}
        thief.close()
        stale = host
        host = connect(URL)
        send(host, {"type": "resume", "code": code, "token": token})
        got = recv(host)
        assert got["type"] == "resumed", f"resume over a live socket: {got}"
        assert recv(viewer)["type"] == "host_back"
        assert recv(host) == {"type": "viewers", "n": 1}
        try:
            got = recv(stale)
            raise AssertionError(f"displaced socket still open: {got}")
        except TimeoutError:
            raise AssertionError("the relay never closed the displaced socket")
        except ConnectionClosed:
            pass
        time.sleep(0.3)                      # its handler has exited
        send(host, {"type": "state", "ck": "state", "seq": 4, "pos": 150.0,
                    "utc": 173.0, "playing": True, "default_delay": 9})
        got = recv(viewer)
        assert got.get("seq") == 4, got      # no host_away from the old one
        print("stale host socket: displaced by its token, and closed")

        # ...and one that never answers the relay's close must not hold up
        # the resume: a client gives up on an attempt after 10 s, which is
        # also the relay's close timeout
        quiet = quiet_link({"type": "resume", "code": code, "token": token})
        assert recv(viewer)["type"] == "host_back"    # quiet holds the room
        host.close()
        host = connect(URL)
        send(host, {"type": "resume", "code": code, "token": token})
        try:
            got = recv(host, timeout=3)
        except TimeoutError:
            raise AssertionError("resume waited on the dead socket's close")
        assert got["type"] == "resumed", got
        assert recv(viewer)["type"] == "host_back"
        assert recv(host) == {"type": "viewers", "n": 1}
        quiet.sendall(bytes([0x88, 0x80]) + os.urandom(4))  # answer at last
        while quiet.recv(4096):              # until the relay hangs up
            pass
        quiet.close()
        time.sleep(0.3)
        send(host, {"type": "state", "ck": "state", "seq": 5, "pos": 160.0,
                    "utc": 183.0, "playing": True, "default_delay": 9})
        got = recv(viewer)
        assert got.get("seq") == 5, got
        print("unresponsive host socket: displaced without waiting on it")

        # a late joiner after all that: cache holds only the known keys
        late = connect(URL)
        send(late, {"type": "join", "code": code, "password": "swordfish"})
        kinds = sorted(recv(late)["type"] for _ in range(4))
        assert kinds == ["fp_chunk", "fp_meta", "joined", "state"], kinds
        try:
            extra = recv(late, timeout=0.5)
            raise AssertionError(f"unexpected cached message: {extra}")
        except TimeoutError:
            pass
        late.close()
        recv(host)                           # viewers: 2
        recv(host)                           # viewers: 1
        print("cache: only state / fp_meta / fp_chunk_N are kept")

        # a host that leaves without coming back ends it after the grace
        host.close()
        assert recv(viewer)["type"] == "host_away"
        assert recv(viewer, timeout=6)["type"] == "ended"
        viewer.close()
        print("teardown: grace period expired -> viewers told it ended")

        # a host that says "end" ends it immediately
        host = connect(URL)
        send(host, {"type": "create", "password": None, "meta": {}})
        code2 = recv(host)["code"]
        v2 = connect(URL)
        send(v2, {"type": "join", "code": code2, "password": None})
        assert recv(v2)["type"] == "joined"
        send(host, {"type": "end"})
        assert recv(v2, timeout=2)["type"] == "ended"
        v2.close()
        host.close()
        print("explicit end: viewers told at once")

        print("RELAY TEST PASSED")
    finally:
        relay.terminate()


if __name__ == "__main__":
    main()
