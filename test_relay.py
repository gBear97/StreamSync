"""End-to-end relay test on localhost: rooms, passwords, caching for late
joiners, host->viewer broadcast, viewer->host routing, session teardown."""

import json
import subprocess
import sys
import time

from websockets.sync.client import connect

PORT = 8899
URL = f"ws://127.0.0.1:{PORT}"


def send(ws, obj):
    ws.send(json.dumps(obj))


def recv(ws, timeout=5):
    return json.loads(ws.recv(timeout=timeout))


def main():
    relay = subprocess.Popen([sys.executable, "relay_server.py",
                              "--port", str(PORT)])
    try:
        time.sleep(1.5)

        host = connect(URL)
        send(host, {"type": "create", "password": "swordfish",
                    "meta": {"title": "test film", "duration": 5400}})
        created = recv(host)
        assert created["type"] == "created", created
        code = created["code"]
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

        # host leaving ends the session for viewers
        host.close()
        assert recv(viewer)["type"] == "ended"
        viewer.close()
        print("teardown: viewers told the session ended")

        print("RELAY TEST PASSED")
    finally:
        relay.terminate()


if __name__ == "__main__":
    main()
