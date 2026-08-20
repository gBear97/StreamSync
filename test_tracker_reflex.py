"""The reflex layer: sub-second pause/resume follow in _auto_loop.

The tracker lives line-for-line in BOTH shells (app.App on Windows,
mac_app.MacApp on macOS), so the scenarios run against both: with no
argument this script re-runs itself once per shell ("windows", "mac") in
fresh processes; a shell whose stack this machine cannot import SKIPs.

Same harness pattern as test_tracker_giveup.py - a FakeListener hands
scripted 0.25s chunks to the REAL _auto_loop - plus two ideas that
make the reflex deterministic:

  * one virtual clock drives both the fake player and a stubbed
    _corr_block, so "the film is playing/advancing" is a scripted fact,
    not a property of random audio;
  * chunks are band-SHAPED, not just loud or quiet: voice(amp) carries
    300-1500Hz only (a talking streamer over a paused film), loud() is
    full-spectrum (film playing).

Scenarios:
  R1: film leaves the mix under talk -> freeze within 2 chunks, silent
      probation, then ONE announced pause (swap+status) at the deadline.
  R2: a false freeze is contradicted by mere lock-grade ADVANCING
      evidence -> resumed + absorbed, nothing announced, and the reflex
      stays locked out until locks re-earn it.
  R3: proven pause, film energy returns -> optimistic resume within 2
      chunks, confirmed by a lock -> swap back + "resumed" status.
  R4: the energy was not the film -> re-pause at the deadline, cooldown,
      at most two optimistic attempts per episode.
  R5: bass-less film: loud talk keeps the high band disarmed (no false
      pause); quiet talk lets it arm and the pause still fires.
  R6: interruption during probation -> held carryover -> proven pause ->
      watcher resume. No orphaned freeze.
  R7: shadow mode logs would-fire decisions and never touches the player.
"""
import logging
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

SHELLS = ("windows", "mac")
shell = next((a for a in sys.argv[1:] if a in SHELLS), None)
if shell is None:
    # run once per shell, each in a fresh process so the monkeypatching
    # (time.sleep, decode_audio, Listener, _corr_block) starts clean
    code = 0
    for name in SHELLS:
        print(f"--- shell: {name}", flush=True)
        code = max(code, subprocess.call([sys.executable, __file__, name]))
    sys.exit(code)

try:
    if shell == "mac":
        import mac_app as S
        AppShell = S.MacApp
    else:
        import app as S
        AppShell = S.App
    import audio_capture
    import audio_matcher
    import matcher
    import players
except Exception as e:   # not this machine's stack (no VLC, no soundcard)
    print(f"SKIPPED: {shell} app stack unavailable ({e})")
    sys.exit(0)

# One virtual clock drives the fake player AND the stubbed correlator, so
# the platform's real output lag has no business in the arithmetic: on
# macOS the 0.17s constant would bias every scripted err and trip the
# micro-absorber counts the scenarios assert on.
players.CLOCK_OUTPUT_LAG = 0.0

_real_sleep = time.sleep
_real_time = time.time
rng = np.random.default_rng(11)

CSR = audio_capture.CAPTURE_SR
CHUNK_N = int(S.TRK_R_CHUNK * CSR)
OFF = 0.3                 # listener timestamps lag perf_counter by this

# small, fast constants - all read by the loop at call time
S.TRK_SEED_N = 3
S.TRK_R_SEED_N = 3
S.TRK_R_CONFIRM = 0.6
S.TRK_R_VERIFY = 0.6
S.TRK_R_COOLDOWN = 0.5
S.TRK_R_CEIL_N = 8

time.sleep = lambda s: _real_sleep(0.001)


def band_noise(amp, f0, f1, n=CHUNK_N, sr=CSR):
    x = rng.standard_normal(n)
    X = np.fft.rfft(x)
    k = np.arange(len(X)) * sr / n
    X[(k < f0) | (k >= f1)] = 0
    y = np.fft.irfft(X, n)
    y *= amp / max(float(np.sqrt(np.mean(y * y))), 1e-12)
    return y.astype(np.float32)


def loud(amp=0.1):
    return (amp * rng.standard_normal(CHUNK_N)).astype(np.float32)


def quiet():
    return (1e-7 * rng.standard_normal(CHUNK_N)).astype(np.float32)


def voice(amp=0.05):
    return band_noise(amp, 300.0, 1500.0)


def voice2(amp=0.02):     # a voice with real upper-mid reach
    return band_noise(amp, 300.0, 2500.0)


def midloud(amp=0.1):     # film-shaped capture for the mono-master film
    return band_noise(amp, 300.0, 4000.0)


FILM_FULL = (0.05 * rng.standard_normal(700000)).astype(np.float32)
FILM_NOBASS = band_noise(0.05, 150.0, 7900.0, n=700000, sr=16000)
FILM_MIDONLY = band_noise(0.05, 300.0, 4000.0, n=700000, sr=16000)
FILM = {"cur": FILM_FULL}


def fake_decode(path, t0_abs, dur, sr=audio_matcher.SR):
    return FILM["cur"][:int(dur * sr)].copy()


audio_matcher.decode_audio = fake_decode
matcher.probe = lambda path: (7200.0, 0.0)
AppShell._wide_relock = lambda self, blocks, pp: None


class Clock:
    """One truth for 'where the stream is': drives the fake player's
    clock and the stubbed correlator, so err is ~0 whenever both run."""

    def __init__(self, base=600.0):
        self.base = base
        self.start = time.perf_counter()

    def now(self):
        return self.base + (time.perf_counter() - self.start)


CLK = Clock()
CORR = {"mode": "lock"}


def fake_corr(ref_t0, W, C):
    m = CORR["mode"]
    if m == "lock":
        return CLK.now() - OFF, 0.80
    if m == "advance":            # film playing, buried under talk
        return CLK.now() - OFF, 0.30
    return 700.0, 0.05            # dead: no useful correlation


AppShell._corr_block = staticmethod(fake_corr)


POISON = object()          # scripted device death: read() raises on it


class FakeListener:
    script = queue.Queue()

    def __init__(self, name=None, sr=CSR):
        pass

    def read(self, seconds):
        assert abs(seconds - S.TRK_R_CHUNK) < 1e-9
        item = FakeListener.script.get()
        if item is POISON:
            raise RuntimeError("loopback capture failed (scripted)")
        return item, time.perf_counter() - OFF

    def close(self):
        pass


audio_capture.Listener = FakeListener


class FakePlayer:
    def __init__(self):
        self.paused = False
        self.calls = []

    def is_playing(self):
        return not self.paused

    def time(self):
        return CLK.now()

    def pause(self):
        self.paused = True
        self.calls.append(("pause",))

    def resume(self):
        self.paused = False
        self.calls.append(("resume",))

    def absorb_drift(self, d):
        self.calls.append(("absorb", d))
        return True

    def sync_seek(self, t, t0, offset):
        self.paused = False
        self.calls.append(("seek", t))

    def n(self, kind):
        return len([c for c in self.calls if c[0] == kind])


class FakeApp(AppShell):
    def __init__(self, mode="live"):
        self.q = queue.Queue()
        self._closing = False
        self.busy = False
        self.auto_enabled = True
        self.auto_follow = True
        self.auto_interval = 1
        self.audio_device = ""
        self.video_path = r"D:\film.mkv"
        self.offset = 0.0
        self.session = None
        self.reflex_mode = mode
        self.active_player = FakePlayer()
        self.embedded = self.active_player   # the reflex is embedded-only


records = []


class Capture(logging.Handler):
    def emit(self, r):
        records.append(r)


S.log.addHandler(Capture())
S.log.setLevel(logging.DEBUG)


def logged(needle):
    return [r for r in records if needle in r.getMessage()]


def wait_for(cond, what, timeout=8.0):
    end = _real_time() + timeout
    while not cond():
        if _real_time() > end:
            raise AssertionError("timeout waiting for " + what)
        _real_sleep(0.01)


def put_chunks(items):
    for c in items:
        FakeListener.script.put(c)
    wait_for(FakeListener.script.empty, "chunks to be consumed")
    _real_sleep(0.12)


def feed_blocks(n, mk=loud):
    put_chunks([mk() for _ in range(4 * n)])


class Scenario:
    def __init__(self, name, mode="live", film=FILM_FULL):
        self.name = name
        FakeListener.script = queue.Queue()
        FILM["cur"] = film
        CORR["mode"] = "lock"
        records.clear()
        self.fake = FakeApp(mode)
        self.player = self.fake.active_player
        self.msgs = []
        self._stop = threading.Event()
        self.pump = threading.Thread(target=self._pump, daemon=True)
        self.pump.start()
        self.loop = threading.Thread(target=self.fake._auto_loop,
                                     daemon=True)
        self.loop.start()

    def _pump(self):
        while not self._stop.is_set():
            try:
                kind, *payload = self.fake.q.get(timeout=0.05)
            except queue.Empty:
                continue
            self.msgs.append((kind, payload))
            if kind == "auto_off":
                self.fake.auto_enabled = False

    def kinds(self, k):
        return [p for kk, p in list(self.msgs) if kk == k]

    def statuses(self, needle):
        return [p[0] for p in self.kinds("status") if needle in p[0]]

    def warmup(self, bands=(0, 1), mk=loud):
        """Prime, then lock until the full-band gain and the reflex band
        gains this film can support calibrate."""
        feed_blocks(8, mk=mk)
        wait_for(lambda: logged("gain calibrated"), "full gain seed")
        for b in bands:
            wait_for(lambda b=b: logged(f"band {b} gain calibrated"),
                     f"band {b} reflex gain")

    def close(self):
        self.fake._closing = True
        for _ in range(4):
            FakeListener.script.put(loud())
        self.loop.join(5.0)
        alive = self.loop.is_alive()
        self._stop.set()
        self.pump.join(2.0)
        assert not alive, f"{self.name}: auto loop failed to exit"
        print(f"PASS {self.name}")


# --- R1: freeze fast and silently; announce only at the deadline
s = Scenario("R1: talk-masked pause freezes in 2 chunks, announces once")
s.warmup()
put_chunks([voice(), voice()])       # streamer talks, film gone
wait_for(lambda: s.player.n("pause") == 1, "the reflex freeze")
assert not s.kinds("swap"), "probation must stay silent"
assert not s.statuses("pausing to match"), "probation must stay silent"
CORR["mode"] = "dead"                # nothing contradicts the freeze
put_chunks([voice(), voice()])       # completes the block: probation runs
_real_sleep(S.TRK_R_CONFIRM + 0.2)
put_chunks([voice()])                # a tick past the deadline promotes
wait_for(lambda: s.kinds("swap"), "the promotion swap")
assert s.kinds("swap")[0] == [True]
assert len(s.statuses("pausing to match")) == 1
assert s.player.n("pause") == 1
assert logged("reflex: pause confirmed")

# --- R3 (same episode): film comes back -> optimistic resume, confirmed
CORR["mode"] = "dead"
feed_blocks(1, mk=voice)             # paused block: watcher ref rebuilds
put_chunks([loud(), loud()])         # film energy returns at the point
wait_for(lambda: s.player.n("seek") == 1, "the optimistic resume seek")
assert not s.statuses("stream resumed"), "resume must confirm first"
CORR["mode"] = "lock"
put_chunks([loud(), loud()])         # completes a block: lock confirms
wait_for(lambda: s.statuses("stream resumed"), "the confirmed resume")
assert [False] in s.kinds("swap")
assert logged("reflex: resume confirmed")
s.close()

# --- R2: lock-grade advancing evidence undoes a false freeze
s = Scenario("R2: advancing evidence undoes the freeze, then locks out")
s.warmup()
put_chunks([voice(), voice()])       # a duck/stall fakes a pause
wait_for(lambda: s.player.n("pause") == 1, "the reflex freeze")
CORR["mode"] = "advance"             # the film is still moving under talk
put_chunks([voice()] * 8)            # two probation blocks = two claims
wait_for(lambda: s.player.n("resume") == 1, "the undo resume")
assert s.player.n("absorb") == 1, "the undo must schedule a heal"
assert s.player.calls[[c[0] for c in s.player.calls].index("absorb")][1] \
    <= 1.5
assert not s.kinds("swap") and not s.statuses("pausing to match"), \
    "an undone freeze must never be announced"
assert logged("reflex: false alarm")
CORR["mode"] = "lock"
put_chunks([voice(), voice()])       # collapse again, still locked out
_real_sleep(0.15)
assert s.player.n("pause") == 1, "reflex re-fired inside its lockout"
feed_blocks(6)                       # locks re-earn the arm
put_chunks([voice(), voice()])
wait_for(lambda: s.player.n("pause") == 2, "re-arm after earned locks")
s.close()

# --- R4: a resume that never confirms re-pauses; two attempts max
s = Scenario("R4: unconfirmed resume re-pauses; optimism is capped")
s.warmup()
put_chunks([voice(), voice()])
wait_for(lambda: s.player.n("pause") == 1, "the reflex freeze")
CORR["mode"] = "dead"
put_chunks([voice(), voice()])
_real_sleep(S.TRK_R_CONFIRM + 0.2)
put_chunks([voice()])
wait_for(lambda: s.kinds("swap"), "promotion")
feed_blocks(1, mk=voice)             # watcher ref rebuild
put_chunks([loud(), loud()])         # a soundboard clip fakes the film
wait_for(lambda: s.player.n("seek") == 1, "optimistic attempt #1")
feed_blocks(1, mk=voice)             # verify window: no lock arrives
_real_sleep(S.TRK_R_VERIFY + 0.2)
feed_blocks(1, mk=voice)
wait_for(lambda: s.player.n("pause") == 2, "the re-pause")
assert not s.statuses("stream resumed")
put_chunks([loud(), loud()])         # inside the cooldown: no attempt
_real_sleep(0.1)
assert s.player.n("seek") == 1, "cooldown ignored"
_real_sleep(S.TRK_R_COOLDOWN)
put_chunks([loud(), loud()])         # attempt #2
wait_for(lambda: s.player.n("seek") == 2, "optimistic attempt #2")
feed_blocks(1, mk=voice)
_real_sleep(S.TRK_R_VERIFY + 0.2)
feed_blocks(1, mk=voice)
wait_for(lambda: s.player.n("pause") == 3, "the second re-pause")
_real_sleep(S.TRK_R_COOLDOWN + 0.1)
put_chunks([loud(), loud(), loud(), loud()])   # episode cap: no third try
_real_sleep(0.15)
assert s.player.n("seek") == 2, "third optimistic attempt must not fire"
s.close()

# --- R9: the slow layer's energy check overrules a bad optimistic
# resume - one handover, no duplicate announcements, no stale deadline
s = Scenario("R9: miss-path overrules a resume; no stale deadline")
S.TRK_R_VERIFY = 5.0     # deadline far away: the miss path must win
s.warmup()
put_chunks([voice(), voice()])
wait_for(lambda: s.player.n("pause") == 1, "the reflex freeze")
CORR["mode"] = "dead"
put_chunks([voice(), voice()])
_real_sleep(S.TRK_R_CONFIRM + 0.2)
put_chunks([voice()])
wait_for(lambda: s.kinds("swap"), "promotion")
feed_blocks(1, mk=voice)             # watcher ref rebuild
put_chunks([loud(), loud()])         # fake film energy: optimistic seek
wait_for(lambda: s.player.n("seek") == 1, "optimistic attempt")
feed_blocks(3, mk=quiet)             # dead air: the boundary block is
                                     # mixed (deg); the next two are the
                                     # energy misses that overrule
wait_for(lambda: s.player.n("pause") == 2, "the miss-path re-pause")
assert len(s.statuses("pausing to match")) == 1, \
    "an already-announced pause must not announce again"
assert [True] == s.kinds("swap")[0] and len(s.kinds("swap")) == 1
assert not logged("resume not confirmed"), \
    "the deadline should never fire once the miss path took over"
assert logged("overruled"), "the handover should be logged"
put_chunks([loud(), loud()])         # cooldown must hold optimism back
_real_sleep(0.15)
assert s.player.n("seek") == 1, "cooldown ignored after the overrule"
_real_sleep(S.TRK_R_COOLDOWN)
CORR["mode"] = "lock"
feed_blocks(4, mk=loud)              # the stream truly resumes
wait_for(lambda: s.statuses("stream resumed"), "the confirmed resume")
CORR["mode"] = "dead"
feed_blocks(2, mk=loud)              # degraded blocks after the resume:
_real_sleep(0.2)                     # a stale deadline would pause here
assert s.player.paused is False, \
    "stale r_try re-paused a genuinely resumed film"
S.TRK_R_VERIFY = 0.6
s.close()

# --- R10: the listener dies mid-verification - ownership falls back to
# the proven pause instead of the film free-running
s = Scenario("R10: listener death mid-verify re-parks the pause")
S.TRK_R_VERIFY = 5.0
s.warmup()
put_chunks([voice(), voice()])
wait_for(lambda: s.player.n("pause") == 1, "the reflex freeze")
CORR["mode"] = "dead"
put_chunks([voice(), voice()])
_real_sleep(S.TRK_R_CONFIRM + 0.2)
put_chunks([voice()])
wait_for(lambda: s.kinds("swap"), "promotion")
feed_blocks(1, mk=voice)
put_chunks([loud(), loud()])
wait_for(lambda: s.player.n("seek") == 1, "optimistic attempt")
FakeListener.script.put(POISON)      # the capture device dies
wait_for(lambda: s.player.n("pause") == 2, "the ownership fallback pause")
assert s.player.paused is True
assert logged("verification interrupted"), "the fallback should be logged"
CORR["mode"] = "lock"
feed_blocks(4, mk=loud)              # listener reopens; stream resumes
wait_for(lambda: s.statuses("stream resumed"), "the watcher recovery")
assert [False] in s.kinds("swap")
S.TRK_R_VERIFY = 0.6
s.close()

# --- R5: bass-less film - the voice-tilt bar adapts arming
s = Scenario("R5: bass-less film arms only against quiet enough talk",
             film=FILM_NOBASS)
s.warmup(bands=(1,))     # no sub-bass in this master: only 4-7k learns
put_chunks([voice(0.15), voice(0.15)])   # SHOUTING: high band must not arm
_real_sleep(0.15)
assert s.player.n("pause") == 0, \
    "reflex paused on loud talk over a bass-less film"
put_chunks([voice(0.01), voice(0.01)])   # quiet talk: film clearly missing
wait_for(lambda: s.player.n("pause") == 1, "the quiet-talk freeze")
s.close()

# --- R6: interruption during probation -> held -> watcher resume
s = Scenario("R6: probation survives an interruption via held")
s.warmup()
put_chunks([voice(), voice()])
wait_for(lambda: s.player.n("pause") == 1, "the reflex freeze")
CORR["mode"] = "dead"
s.fake.busy = True                   # a manual sync barges in
put_chunks([voice()])                # one chunk cycles the loop into its
_real_sleep(0.15)                    # guard: held captured, listener closed
s.fake.busy = False
CORR["mode"] = "lock"                # the stream film is audible again
feed_blocks(4)                       # prime + two coherent watcher claims
wait_for(lambda: s.player.n("seek") >= 1, "the watcher resume seek")
wait_for(lambda: s.statuses("stream resumed"), "the resume status")
assert s.player.paused is False
s.close()

# --- R8: an old mono master has ONLY upper mids - they arm via learned
# talk ceilings after the first (slow-layer) pause, and the NEXT pause
# freezes at reflex speed
s = Scenario("R8: mid-band arms via the streamer's learned talk ceiling",
             film=FILM_MIDONLY)
s.warmup(bands=(2,), mk=midloud)
put_chunks([voice2(0.02), voice2(0.02)])   # talk-masked pause, no ceiling
_real_sleep(0.15)                          # yet: the mid band must NOT arm
assert s.player.n("pause") == 0, "mid band armed before any ceiling"
CORR["mode"] = "dead"
feed_blocks(2, mk=lambda: voice2(0.005))   # near-quiet talk: the slow
                                           # layer's energy misses fire
wait_for(lambda: s.player.n("pause") == 1, "the slow-layer pause")
wait_for(lambda: s.kinds("swap"), "the announced slow pause")
feed_blocks(10, mk=lambda: voice2(0.01))   # pause talk teaches ceilings
CORR["mode"] = "lock"
feed_blocks(3, mk=midloud)                 # stream resumes; watcher seeks
wait_for(lambda: s.statuses("stream resumed"), "the watcher resume")
feed_blocks(6, mk=midloud)                 # locks re-anchor, lag settles
put_chunks([voice2(0.005), voice2(0.005)])
wait_for(lambda: s.player.n("pause") == 2, "the mid-band reflex freeze")
assert logged("(bands [2])"), "the freeze should credit the mid band"
s.close()

# --- R7: shadow mode observes, never acts
s = Scenario("R7: shadow mode logs and touches nothing", mode="shadow")
s.warmup()
put_chunks([voice(), voice(), voice(), voice()])
wait_for(lambda: logged("reflex[shadow]: would freeze"),
         "the shadow would-freeze log")
assert s.player.n("pause") == 0 and s.player.n("seek") == 0
s.close()

print("ALL REFLEX SCENARIOS PASS")
