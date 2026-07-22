"""End-to-end relay test on localhost: rooms, passwords, caching for late
joiners, host->viewer broadcast, viewer->host routing, host reconnection
across a dropped socket, and session teardown."""

import json
import queue
import subprocess
import sys
import time

from websockets.sync.client import connect

import session

PORT = 8899
URL = f"ws://127.0.0.1:{PORT}"
GRACE = 3.0          # short host-grace so the expiry test does not crawl


def send(ws, obj):
    ws.send(json.dumps(obj))


def recv(ws, timeout=5):
    return json.loads(ws.recv(timeout=timeout))


def drain(events, until, timeout=15.0):
    """Collect ("session", text) messages until `until()` and then quiet."""
    out, deadline = [], time.time() + timeout
    while time.time() < deadline:
        try:
            out.append(events.get(timeout=0.2)[1])
        except queue.Empty:
            if until():
                break
    return out


def main():
    relay = subprocess.Popen([sys.executable, "relay_server.py",
                              "--port", str(PORT),
                              "--host-grace", str(GRACE)])
    try:
        time.sleep(1.5)

        host = connect(URL)
        send(host, {"type": "create", "password": "swordfish",
                    "meta": {"title": "test film", "duration": 5400}})
        created = recv(host)
        assert created["type"] == "created", created
        code, token = created["code"], created["token"]
        assert token, "host was issued no resume token"
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

        # a dropped host does NOT end the party - the room is held open
        host.close()
        assert recv(viewer)["type"] == "host_gone"
        print("host dropped: viewers held, not ended")

        # ...and only the real host can claim it back
        impostor = connect(URL)
        send(impostor, {"type": "resume", "code": code, "token": "guess"})
        assert recv(impostor)["reason"] == "bad host token"
        impostor.close()
        print("resume: wrong token rejected")

        host = connect(URL)
        send(host, {"type": "resume", "code": code, "token": token})
        resumed = recv(host)
        assert resumed["type"] == "resumed" and resumed["code"] == code, resumed
        assert recv(viewer)["type"] == "host_back"
        send(host, {"type": "state", "ck": "state", "seq": 2, "pos": 200.0,
                    "utc": 223.0, "playing": True, "default_delay": 9})
        back = recv(viewer)
        assert back["seq"] == 2 and back["playing"] is True, back
        print("resume: host reconnected, same room, state flowing again")

        # a host that never comes back does end the session, after the grace
        host.close()
        assert recv(viewer)["type"] == "host_gone"
        assert recv(viewer, timeout=GRACE + 5)["type"] == "ended"
        viewer.close()
        print(f"expiry: session ended {GRACE:.0f}s after the host stayed away")

        # deliberate teardown skips the grace period entirely
        host2 = connect(URL)
        send(host2, {"type": "create", "password": None, "meta": {}})
        code2 = recv(host2)["code"]
        v2 = connect(URL)
        send(v2, {"type": "join", "code": code2, "password": None})
        assert recv(v2)["type"] == "joined"
        assert recv(host2)["type"] == "viewers"
        t0 = time.time()
        send(host2, {"type": "end"})
        assert recv(v2)["type"] == "ended"
        assert time.time() - t0 < GRACE, "explicit end waited for the grace"
        host2.close(); v2.close()
        print("explicit end: viewers stopped immediately")

        # a viewer whose socket blips rejoins on its own, without re-verifying
        h3 = connect(URL)
        send(h3, {"type": "create", "password": None, "meta": {}})
        code3 = recv(h3)["code"]
        send(h3, {"type": "fp_meta", "ck": "fp_meta", "chunks": 1, "words": 4,
                  "duration": 100, "title": "t"})
        send(h3, {"type": "fp_chunk", "ck": "fp_chunk_0", "i": 0,
                  "data": "AAABAAIAAwA="})
        v = session.ViewerSession(URL, code3, "unused.mkv", None, queue.Queue())
        v.clock.sync = lambda *a, **k: 0.0
        v.verified = True      # already verified: no real film file here
        v._workers = True      # and no audio devices to measure/drive with
        v.start()
        assert recv(h3, timeout=10)["n"] == 1
        deadline = time.time() + 10
        while v.link is None and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(0.5)
        v.link.ws.close()                      # transient network blip
        assert recv(h3, timeout=10)["n"] == 0, "relay never saw the drop"
        assert recv(h3, timeout=20)["n"] == 1, "viewer never came back"
        assert not v.stop_flag.is_set(), "a blip killed the viewer session"
        v.stop()
        h3.close()
        print("viewer blip: rejoined automatically, session survived")

        # a rejected join must say why, not blame the host's fingerprinting
        v3 = connect(URL)
        send(v3, {"type": "create", "password": "swordfish", "meta": {}})
        live_code = recv(v3)["code"]
        bad = session.ViewerSession(URL, live_code, "unused.mkv", None,
                                    queue.Queue(), password="wrong")
        bad.clock.sync = lambda *a, **k: 0.0     # no NTP in tests
        bad.start()
        msgs = drain(bad.events, bad.stop_flag.is_set)
        assert any("wrong password" in m for m in msgs), msgs
        assert not any("never arrived" in m for m in msgs), msgs
        assert bad.stop_flag.wait(5), "viewer never stopped after a hard error"
        v3.close()
        print("bad password: reported as a bad password, and the viewer stops")

        print("RELAY TEST PASSED")
    finally:
        relay.terminate()


if __name__ == "__main__":
    main()
