"""Give-up behavior of the auto tracker's failure handler (app._auto_loop).

The harness pattern: a FakeListener hands scripted capture blocks to the
REAL App._auto_loop running on a real worker thread, and
audio_matcher.decode_audio is monkeypatched to fail the way a vanished
film file fails (drive unplugged, file moved, dead ffmpeg). No Tk exists:
a tiny pump plays _poll_queue's part - on "auto_off" it flips
auto_enabled False, which is what the real handler's untick does.

Scenarios:
  A: the same failure three checks running -> ONE raw status, ONE
     traceback in the log, ONE clear auto_off with the file hint, and
     the loop stops churning (no further log growth).
  B: two failures, then clean checks, then two more failures -> no
     give-up (the clean check broke the streak); a third consecutive
     failure then fires it.
  C: three failures with ALTERNATING signatures -> no give-up.
  D: a non-file error repeated three times -> generic wording, still off.
"""
import logging
import queue
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

try:
    import app
    import audio_capture
    import audio_matcher
    import matcher
except Exception as e:   # not this machine's stack (no VLC, no soundcard)
    print(f"SKIPPED: app stack unavailable ({e})")
    sys.exit(0)

_real_sleep = time.sleep
_real_time = time.time
rng = np.random.default_rng(7)

sleeps = []


def _fast_sleep(s):
    sleeps.append(s)
    _real_sleep(0.001)


time.sleep = _fast_sleep


def noise(n=audio_capture.CAPTURE_SR):
    return (0.1 * rng.standard_normal(n)).astype(np.float32)


DECODE = {"mode": "fnf"}


def fake_decode(path, t0_abs, dur, sr=audio_matcher.SR):
    mode = DECODE["mode"]
    if mode == "fnf":
        raise FileNotFoundError(2, "The system cannot find the file specified")
    if mode == "alt":
        raise RuntimeError("loopback device wedged")
    return (0.05 * rng.standard_normal(int(dur * sr))).astype(np.float32)


audio_matcher.decode_audio = fake_decode
matcher.probe = lambda path: (7200.0, 0.0)


class FakeListener:
    script = queue.Queue()

    def __init__(self, name=None, sr=audio_capture.CAPTURE_SR):
        pass

    def read(self, seconds):
        return FakeListener.script.get(), time.perf_counter() - 1.0

    def close(self):
        pass


audio_capture.Listener = FakeListener


class FakePlayer:
    def __init__(self):
        self.t = 300.0
        self.calls = []

    def is_playing(self):
        return True

    def time(self):
        self.t += 1.0
        return self.t

    def absorb_drift(self, d):
        self.calls.append(("absorb", d))

    def sync_seek(self, *a):
        self.calls.append(("seek", a))

    def pause(self):
        self.calls.append(("pause",))


class FakeApp(app.App):
    def __init__(self):   # the real one builds the whole UI - skip it
        self.q = queue.Queue()
        self._closing = False
        self.busy = False
        self.auto_enabled = True
        self.auto_follow = False
        self.auto_interval = 1        # retry sleeps floor at max(., 10)
        self.audio_device = ""
        self.video_path = r"D:\gone\film.mkv"
        self.offset = 0.0
        self.session = None
        self.active_player = FakePlayer()
        self.embedded = object()      # is-not active_player -> no clock lag


records = []


class Capture(logging.Handler):
    def emit(self, r):
        records.append(r)


app.log.addHandler(Capture())
app.log.setLevel(logging.DEBUG)


def tracebacks():
    return [r for r in records if r.exc_info]


def wait_for(cond, what, timeout=8.0):
    end = _real_time() + timeout
    while not cond():
        if _real_time() > end:
            raise AssertionError("timeout waiting for " + what)
        _real_sleep(0.01)


def feed(n=1):
    """Feed n capture blocks and wait until the loop has taken them all."""
    for _ in range(n):
        FakeListener.script.put(noise())
    wait_for(FakeListener.script.empty, "blocks to be consumed")
    _real_sleep(0.1)      # let the iteration holding the last block finish


class Scenario:
    def __init__(self, name):
        self.name = name
        FakeListener.script = queue.Queue()
        DECODE["mode"] = "fnf"
        records.clear()
        sleeps.clear()
        self.fake = FakeApp()
        self.msgs = []
        self._stop = threading.Event()
        self.pump = threading.Thread(target=self._pump, daemon=True)
        self.pump.start()
        self.loop = threading.Thread(target=self.fake._auto_loop, daemon=True)
        self.loop.start()

    def _pump(self):
        while not self._stop.is_set():
            try:
                kind, *payload = self.fake.q.get(timeout=0.05)
            except queue.Empty:
                continue
            self.msgs.append((kind, payload))
            if kind == "auto_off":
                self.fake.auto_enabled = False   # what the Tk untick does

    def kinds(self, k):
        return [p for kk, p in list(self.msgs) if kk == k]

    def statuses(self, needle):
        return [p[0] for p in self.kinds("status") if needle in p[0]]

    def close(self):
        self.fake._closing = True
        FakeListener.script.put(noise())   # unblock a parked read
        self.loop.join(5.0)
        alive = self.loop.is_alive()
        self._stop.set()
        self.pump.join(2.0)
        assert not alive, f"{self.name}: auto loop failed to exit"
        print(f"PASS {self.name}")


# --- A: identical permanent failure -> one status, one traceback, one off
s = Scenario("A: permanent identical failure gives up cleanly")
feed(2)                  # film probe + pair prime: clean iterations
feed(3)                  # three identical FileNotFoundError checks
wait_for(lambda: s.kinds("auto_off"), "the give-up message")
raw = s.statuses("Auto-resync check failed")
assert len(raw) == 1, f"raw error status shown {len(raw)}x: {raw}"
offs = s.kinds("auto_off")
assert len(offs) == 1, offs
msg = offs[0][0]
assert msg.startswith("Auto tracking stopped: can't read the film's audio"), msg
assert "Re-tick" in msg, msg
assert len(tracebacks()) == 1, \
    f"traceback logged {len(tracebacks())}x, want once"
assert sleeps.count(10) == 2, \
    f"expected two retry waits at the 10s floor, got {sleeps}"
assert not s.fake.active_player.calls, s.fake.active_player.calls
n_rec = len(records)
_real_sleep(0.4)         # parked: neither the log nor the queue may grow
assert len(records) == n_rec, records[n_rec:]
assert len(s.kinds("status")) == 1
s.close()

# --- B: a clean check breaks the streak
s = Scenario("B: clean check resets the counter")
feed(2)                  # probe + prime
feed(2)                  # fails 1, 2
assert not s.kinds("auto_off"), "gave up after only two failures"
DECODE["mode"] = "ok"
feed(2)                  # clean checks: ref rebuilds fine, streak resets
DECODE["mode"] = "fnf"
s.fake.active_player.t += 120   # kick expect out of the ref window so the
feed(2)                          # next checks decode again: fails 1', 2'
_real_sleep(0.3)
assert not s.kinds("auto_off"), \
    "gave up on the 4th non-consecutive failure: streak did not reset"
feed(1)                  # fail 3' - now three in a row
wait_for(lambda: s.kinds("auto_off"), "the give-up after a real streak")
assert len(tracebacks()) == 2, \
    f"want one traceback per streak (2), got {len(tracebacks())}"
s.close()

# --- C: alternating signatures never accumulate to a give-up
s = Scenario("C: alternating signatures don't accumulate")
feed(2)
DECODE["mode"] = "fnf"
feed(1)
DECODE["mode"] = "alt"
feed(1)
DECODE["mode"] = "fnf"
feed(1)
_real_sleep(0.3)
assert not s.kinds("auto_off"), "alternating failures must not give up"
assert s.fake.auto_enabled, "loop disarmed without posting auto_off?"
s.close()

# --- D: a non-file error gets the generic wording
s = Scenario("D: non-file failures word the stop generically")
DECODE["mode"] = "alt"
feed(2)
feed(3)
wait_for(lambda: s.kinds("auto_off"), "the generic give-up")
msg = s.kinds("auto_off")[0][0]
assert msg.startswith("Auto tracking stopped:"), msg
assert "kept hitting" in msg and "loopback device wedged" in msg, msg
assert "can't read the film's audio" not in msg, msg
s.close()

print("ALL SCENARIOS PASS")
