"""AudioMonitor timing, with a fake device instead of a sound card.

The fake has a true sample clock (sample n exists at t_dev + n/sr) and
hands out blocks late by a random scheduling delay, the way a busy
machine does. The monitor must date a recording by that true clock -
not by whenever its read happened to return - slice exactly the samples
asked for, and refuse a recording that spans a delivery stall (WASAPI
loopback sends nothing at all while nothing plays).

No stamp can beat the promptest block delivery the machine manages, and
that floor is the host's, not the monitor's: a few tenths of a ms on
one box, 3-12 ms on a macOS CI runner whose sleeps all overshoot. So the
test checks what the monitor promises - never earlier than the truth,
and within a hair of the best bound its blocks allow - rather than an
absolute figure that only holds on a quiet machine.
"""

import threading
import time

import numpy as np

from audio_capture import AudioMonitor

SR = 8000


class FakeDevice:
    def __init__(self, jitter=0.03, stall_at=None, stall_s=0.0, seed=1):
        self.rng = np.random.default_rng(seed)
        self.jitter = jitter
        self.stall_at = stall_at      # frame where delivery stops...
        self.stall_s = stall_s        # ...for this long, samples lost
        self.t_dev = None
        self.n = 0
        self.lost = 0.0
        self.lock = threading.Lock()
        self.delivered = []           # (end_frame, perf_counter at return)

    def true_time(self, frame):
        """When `frame` was really captured (perf_counter)."""
        extra = self.stall_s if self.stall_at is not None and frame >= self.stall_at else 0.0
        return self.t_dev + frame / SR + extra

    # context-manager recorder protocol, as soundcard provides
    def __enter__(self):
        self.t_dev = time.perf_counter()
        return self

    def __exit__(self, *exc):
        return False

    def record(self, numframes):
        end = self.n + numframes
        ready = self.true_time(end - 1)
        delay = ready - time.perf_counter() + self.rng.uniform(0, self.jitter)
        if delay > 0:
            time.sleep(delay)
        data = (np.arange(self.n, end, dtype=np.float32) % 1000) / 1000.0 + 0.01
        self.n = end
        self.delivered.append((end, time.perf_counter()))
        return data

    def best_bound(self, frame, by, span=2.0):
        """The earliest time the blocks delivered before `by` prove
        `frame` existed by - all a stamp taken at `by` could know."""
        return min(t - (end - frame) / SR for end, t in self.delivered
                   if frame <= end <= frame + span * SR and t <= by)


def monitor_for(dev):
    return AudioMonitor("loopback", sr=SR, keep=30, source=lambda: (dev, "hint"))


def main():
    # 1. dating: vs the true sample clock, and vs the best bound the
    # delivered blocks allow, over several recordings
    dev = FakeDevice(jitter=0.03)
    mon = monitor_for(dev)
    marks = []
    try:
        for _ in range(4):
            mark = mon.mark()
            data, sr, t0 = mon.capture(0.6, start=mark)
            returned = time.perf_counter()
            assert sr == SR and data.size == int(0.6 * SR)
            # samples are contiguous and in order
            want = (np.arange(data.size) + (mark % 1000)) % 1000 / 1000.0 + 0.01
            assert np.allclose(data, want, atol=1e-4), "slice is not contiguous"
            marks.append((mark, t0, returned))
        # instant capture of already-buffered audio
        time.sleep(0.2)
        data, _, t0 = mon.capture(1.0)
        assert data.size == SR
    finally:
        mon.stop()
    vs_truth = [1000 * (t0 - dev.true_time(m)) for m, t0, _ in marks]
    vs_best = [1000 * (t0 - dev.best_bound(m, by)) for m, t0, by in marks]
    print("stamp vs true clock (ms): " + ", ".join(f"{e:+.1f}" for e in vs_truth))
    print("stamp vs best possible (ms): " + ", ".join(f"{e:+.2f}" for e in vs_best))
    # never early (a sample cannot be dated before it existed)...
    assert all(e > -1.0 for e in vs_truth), vs_truth
    # ...and as tight as the deliveries allow: the monitor reads the clock
    # a moment after the device returns, so it may trail the bound slightly
    assert all(-0.5 < e < 2.0 for e in vs_best), vs_best

    # 2. a delivery stall inside the recording is refused
    dev = FakeDevice(jitter=0.005, stall_at=int(0.5 * SR), stall_s=0.6)
    mon = monitor_for(dev)
    try:
        mark = mon.mark()
        try:
            mon.capture(1.0, start=mark)
            raise AssertionError("a recording across a stall was accepted")
        except RuntimeError as e:
            print(f"stall: refused ({e})")
    finally:
        mon.stop()

    # 3. silence is refused on a loopback, allowed when asked
    class Quiet(FakeDevice):
        def record(self, numframes):
            super().record(numframes)
            return np.zeros(numframes, np.float32)
    dev = Quiet(jitter=0.005)
    mon = monitor_for(dev)
    try:
        try:
            mon.capture(0.3, start=mon.mark())
            raise AssertionError("silence was accepted")
        except RuntimeError as e:
            assert "hint" in str(e)
        data, _, _ = mon.capture(0.3, start=mon.mark(), allow_silence=True)
        assert data.size == int(0.3 * SR)
        print("silence: refused on loopback, allowed on request")
    finally:
        mon.stop()

    # 4. a device that fails is reported, not waited on
    def broken():
        raise OSError("device unplugged")
    mon = AudioMonitor("loopback", sr=SR, source=broken)
    try:
        mon.capture(0.2, timeout=2.0)
        raise AssertionError("a failed device was not reported")
    except RuntimeError as e:
        assert "unplugged" in str(e), e
        print(f"broken device: {e}")

    # 4b. a source slow to start (macOS checking a new helper, a permission
    # prompt) is waited for, and its startup does not eat the recording's
    # time; an explicit timeout still means what it says
    class SlowStart(FakeDevice):
        def __enter__(self):
            time.sleep(6.5)               # past the default 0.3 + 5 s
            return super().__enter__()
    mon = monitor_for(SlowStart(jitter=0.005))
    try:
        began = time.monotonic()
        data, _, _ = mon.capture(0.3)
        assert data.size == int(0.3 * SR)
        print(f"slow start: waited {time.monotonic() - began:.1f} s for the source")
    finally:
        mon.stop()
    mon = monitor_for(SlowStart(jitter=0.005))
    try:
        began = time.monotonic()
        mon.capture(0.3, timeout=2.0)
        raise AssertionError("an explicit timeout was not kept")
    except RuntimeError as e:
        assert "in time" in str(e) and time.monotonic() - began < 4.0, e
    finally:
        mon.stop()

    # 4c. a mark counts as a use: marked, then busy a while (a picture
    # timing grabs the screen for 20 s), the audio since the mark is
    # still there, though the device had been idle nearly its limit
    dev = FakeDevice(jitter=0.005)
    mon = AudioMonitor("loopback", sr=SR, keep=30, idle_timeout=1.0,
                       source=lambda: (dev, "hint"))
    try:
        mon.capture(0.2, start=mon.mark())
        time.sleep(0.7)
        mark = mon.mark()
        time.sleep(0.7)                   # past the idle limit, counted from the capture
        data, _, _, lo = mon.capture_span(0.5, start=mark, timeout=2.0)
        # closed and reopened, the monitor would hand back audio from the
        # reopening instead - not what was asked for
        assert lo == mark and data.size == int(0.5 * SR), (lo, mark)
        print("a mark keeps the device open: the audio after it is kept")
    finally:
        mon.stop()

    system_audio_tests()
    source_choice_tests()
    playout_tests()
    windows_source_tests()
    print("AUDIO CAPTURE TEST PASSED")


# --- This Mac's sound: SystemAudioRecorder over a fake sstap ---------------

# Speaks sstap's protocol (mac/sstap.swift) on stdout, with capture times
# from a clock it states in its hello ("test_t0": when sample 0 was
# captured), so the test knows exactly when every sample "happened". It
# stops when its stdin closes, as sstap does.
FAKE_SSTAP = r'''
import json, os, struct, sys, threading, time
import numpy as np

opt = dict(a.split("=", 1) for a in sys.argv[1:])
rate = float(opt.get("rate", 48000))
lat = int(opt.get("lat", 0))
mode = opt.get("mode", "ok")
out = sys.stdout.buffer

def say(obj):
    data = json.dumps(obj).encode()
    out.write(b"SSJ1" + struct.pack("<I", len(data)) + data)
    out.flush()

def stdin_closed():
    sys.stdin.buffer.read()
    os._exit(0)
threading.Thread(target=stdin_closed, daemon=True).start()

if mode == "error":
    say({"type": "error", "code": "tap-failed",
         "message": "macOS would not let StreamSync tap this Mac's sound"})
    sys.exit(2)
t0 = time.perf_counter() + 0.05
say({"type": "hello", "version": 1, "test_t0": t0,
     "tap": {"rate": rate, "aggregate_rate": rate, "channels": 2},
     "output": {"latency": {"device": lat // 2, "safety": 0,
                            "stream": lat - lat // 2, "buffer": 512,
                            "rate": rate}}})
if "playing" in opt:
    say({"type": "status", "playing": opt["playing"] == "1"})
n, block = 0, 512
while True:
    host = t0 + n / rate
    wait = host + block / rate - time.perf_counter()
    if wait > 0:
        time.sleep(wait)
    if mode == "silent":
        x = np.zeros(block, "<f4")
    else:
        x = (0.5 * np.sin(2 * np.pi * 440 * np.arange(n, n + block) / rate)).astype("<f4")
    out.write(b"SSA1" + struct.pack("<IdddII", 0, host, float(n),
                                    time.perf_counter(), block, 3) + x.tobytes())
    # a --compare source's blocks are not the stream's
    out.write(b"SSA1" + struct.pack("<IdddII", 1, host, float(n),
                                    time.perf_counter(), 4, 3) + b"\x00" * 16)
    out.flush()
    n += block
    if mode == "change" and n >= rate * 0.3:
        say({"type": "event", "event": "output-changed"})
        sys.exit(3)
    if mode == "garbage" and n >= rate * 0.3:
        out.write(b"JUNK")
        out.flush()
        time.sleep(5)
'''


def system_audio_tests():
    import os
    import sys
    import tempfile

    import audio_capture as ac

    fake = os.path.join(tempfile.mkdtemp(), "fake_sstap.py")
    with open(fake, "w") as f:
        f.write(FAKE_SSTAP)

    def monitor(sr=48000, **opt):
        cmd = [sys.executable, fake] + [f"{k}={v}" for k, v in opt.items()]
        src = ac.SystemAudioSource(cmd=cmd)
        mon = AudioMonitor("loopback", sr=sr, keep=30,
                           source=lambda: (src.recorder(sr), src.hint))
        return mon, src

    # 5. dated by sstap's capture times plus the output's own delay -
    # exactly, not by when the blocks happened to arrive
    mon, src = monitor(lat=480)                 # 10 ms at 48 kHz
    try:
        data, sr, t0, lo = mon.capture_span(0.5, start=mon.mark())
        hello_t0 = src.rec.hello["test_t0"]
        want = hello_t0 + lo / 48000 + 0.010
        assert abs(t0 - want) < 2e-6, (t0, want)
        k = np.arange(lo, lo + data.size)
        assert np.allclose(data, 0.5 * np.sin(2 * np.pi * 440 * k / 48000),
                           atol=1e-5), "samples were not the stream's"
        print(f"system audio: stamped to {abs(t0 - want) * 1e6:.2f} us of "
              "capture time + output delay")
    finally:
        mon.stop()
    assert src.rec.proc.poll() is not None, "sstap left running after stop()"

    # 6. another output rate is resampled onto the monitor's grid, with
    # the same timing
    mon, src = monitor(sr=48000, rate=44100, lat=441)   # 10 ms at 44.1 kHz
    try:
        data, sr, t0, lo = mon.capture_span(0.5, start=mon.mark())
        first = src.rec.hello["test_t0"] + 0.010
        want = first + lo / 48000
        assert sr == 48000 and data.size == 24000
        assert abs(t0 - want) < 2e-6, (t0, want)
        t = want - 0.010 - src.rec.hello["test_t0"] + np.arange(data.size) / 48000
        assert np.allclose(data, 0.5 * np.sin(2 * np.pi * 440 * t), atol=3e-3)
        print("system audio: 44.1 kHz output resampled to 48 kHz on time")
    finally:
        mon.stop()

    # 7. silence says why: macOS withholding it, or nothing playing here
    for playing, needle in (("1", "System Audio Recording"),
                            ("0", "Nothing is playing on this Mac")):
        mon, src = monitor(mode="silent", playing=playing)
        try:
            mon.capture(0.3, start=mon.mark())
            raise AssertionError("silence was accepted")
        except RuntimeError as e:
            assert needle in str(e), e
        finally:
            mon.stop()
    print("system audio: silence explained (refused permission / nothing playing)")

    # 8. sstap's own error reaches the user
    mon, _ = monitor(mode="error")
    try:
        mon.capture(0.2, timeout=5.0)
        raise AssertionError("an sstap error was not reported")
    except RuntimeError as e:
        assert "would not let StreamSync" in str(e), e
    finally:
        mon.stop()

    # 9. a new sound output ends a recording in progress, and the next
    # sync starts fresh (the monitor restarts sstap)
    mon, src = monitor(mode="change")
    try:
        try:                          # the fake switches outputs at 0.3 s
            mon.capture(1.0, start=mon.mark(), timeout=5.0)
            raise AssertionError("a changed output went unnoticed")
        except RuntimeError as e:
            assert "sound output changed" in str(e), e
        data, _, _ = mon.capture(0.1, start=mon.mark(), timeout=5.0)
        assert data.size == 4800
    finally:
        mon.stop()

    # 10. a garbled stream is an error, not a hang
    mon, _ = monitor(mode="garbage")
    try:
        mon.capture(1.0, start=mon.mark(), timeout=5.0)
        raise AssertionError("garbage from sstap was accepted")
    except RuntimeError as e:
        assert "unexpected" in str(e), e
    finally:
        mon.stop()
    print("system audio: errors, output change and garbage handled")


def source_choice_tests():
    """What Listen to offers, and what each choice opens, on a Mac with
    and without taps - over a fake soundcard, so it runs anywhere."""
    import sys
    import types

    import audio_capture as ac

    class Mic:
        def __init__(self, name):
            self.name = name

    mics = [Mic("MacBook Pro Microphone"), Mic("BlackHole 2ch")]
    fake_sc = types.SimpleNamespace(all_microphones=lambda: list(mics),
                                    default_microphone=lambda: mics[0])
    saved = (sys.modules.get("soundcard"), ac.IS_MAC, ac.macos_version,
             ac.helper_path)
    sys.modules["soundcard"] = fake_sc
    ac.IS_MAC = True
    try:
        # macOS 14.2+: this Mac's sound first, and the default
        ac.macos_version = lambda: (15, 0)
        ac.helper_path = lambda: "/Applications/StreamSync.app/Contents/MacOS/sstap"
        names = ac.list_speakers()
        assert names[0] == ac.SYSTEM_AUDIO and names[1] == "BlackHole 2ch", names
        assert ac.default_speaker_name() == ac.SYSTEM_AUDIO
        src, hint = ac._open_source("loopback", "")
        assert isinstance(src, ac.SystemAudioSource) and callable(hint)
        src, _ = ac._open_source("loopback", "blackhole")
        assert src.name == "BlackHole 2ch"
        # a named device that is gone is said so - never a quiet mic
        mics.remove(mics[1])
        try:
            ac._open_source("loopback", "BlackHole 2ch")
            raise AssertionError("a missing device fell back silently")
        except RuntimeError as e:
            assert "not connected" in str(e), e
        # a source run without the helper built says how to build it
        ac.helper_path = lambda: None
        try:
            ac._open_source("loopback", "")
            raise AssertionError("a missing helper went unexplained")
        except RuntimeError as e:
            assert "build_sstap.sh" in str(e), e

        # before 14.2: BlackHole if it is there, else the reason - not a mic
        ac.macos_version = lambda: (14, 1)
        assert ac.SYSTEM_AUDIO not in ac.list_speakers()
        assert ac.default_speaker_name() == ac.SYSTEM_AUDIO
        try:
            ac._open_source("loopback", "")
            raise AssertionError("an old macOS fell back to the mic")
        except RuntimeError as e:
            assert "14.2" in str(e) and "BlackHole" in str(e), e
        mics.append(Mic("BlackHole 2ch"))
        assert ac.default_speaker_name() == "BlackHole 2ch"
        src, _ = ac._open_source("loopback", "")
        assert src.name == "BlackHole 2ch"
    finally:
        if saved[0] is not None:
            sys.modules["soundcard"] = saved[0]
        else:
            sys.modules.pop("soundcard", None)
        ac.IS_MAC, ac.macos_version, ac.helper_path = saved[1:]
    print("listen-to choices: tap first on 14.2+, BlackHole before it, "
          "never a silent mic")


# --- Windows loopback: WASAPI's playout stamps -----------------------------

class FakeWasapi:
    """soundcard's WASAPI loopback recorder, as PlayoutRecorder uses it:
    one 10 ms packet per _record_chunk(), fetched through _capture_buffer().
    Packet k's first frame plays at play_time(k * N); the packet is handed
    over LEAD before that, late by a random scheduling delay, the way a
    loopback packet reaches StreamSync before the speakers play it. Its
    stamp is that play time in 100 ns units, or what stamp(k, t) says."""

    N = 80                          # 10 ms at SR
    LEAD = 0.030

    def __init__(self, jitter=0.004, stamp=None, silent=None, channels=2, seed=2):
        self.rng = np.random.default_rng(seed)
        self.jitter = jitter
        self.stamp = stamp or (lambda k, t: (int(round(t * 1e7)), 0))
        self.silent = silent or (lambda k: False)   # soundcard's zero-fill
        self.channelmap = list(range(channels))
        self.k = 0
        self.t_play = None
        self.pending = np.zeros(0, np.float32)
        self.delivered = []         # (end_frame, perf_counter at return)

    def play_time(self, frame):
        return self.t_play + frame / SR

    def __enter__(self):
        self.t_play = time.perf_counter() + 0.05
        return self

    def __exit__(self, *exc):
        return False

    def _capture_buffer(self):      # soundcard's own: no timestamp asked for
        return None, self.N, 0

    def _record_chunk(self):
        k = self.k
        t_first = self.play_time(k * self.N)
        wait = t_first - self.LEAD - time.perf_counter() + self.rng.uniform(0, self.jitter)
        if wait > 0:
            time.sleep(wait)
        frames = np.arange(k * self.N, (k + 1) * self.N)
        mono = ((frames % 1000) / 1000.0 + 0.01).astype(np.float32)
        if self.silent(k):
            mono[:] = 0                # no packet: nothing to ask WASAPI about
        else:
            self.current = self.stamp(k, t_first)
            self._capture_buffer()
        self.k += 1
        self.delivered.append(((k + 1) * self.N, time.perf_counter()))
        return np.repeat(mono, len(self.channelmap))      # interleaved

    def record(self, numframes):    # soundcard's record(), for the unhooked path
        nch = len(self.channelmap)
        while self.pending.size < numframes * nch:
            self.pending = np.concatenate([self.pending, self._record_chunk()])
        out, self.pending = self.pending[:numframes * nch], self.pending[numframes * nch:]
        return out.reshape(-1, nch)

    def arrival_bound(self, frame, span=2.0):
        """The tightest arrival-based bound - what stamping on arrival gives."""
        return min(t - (end - frame) / SR for end, t in self.delivered
                   if frame <= end <= frame + span * SR)


def fake_hook(rec, note):
    def capture_buffer():
        note(*rec.current)
        return None, rec.N, rec.current[1]
    rec._capture_buffer = capture_buffer


def playout_tests():
    import audio_capture as ac

    def run(dev, hook=fake_hook, seconds=0.6):
        logs = []
        recs = []

        def source():
            recs.append(ac.PlayoutRecorder(dev, SR, name="Speakers (fake)",
                                           hook=hook, log=logs.append))
            return recs[-1], "hint"
        mon = AudioMonitor("loopback", sr=SR, keep=30, source=source)
        try:
            data, sr, t0, lo = mon.capture_span(seconds, start=mon.mark(), allow_silence=True)
            time.sleep(0.6)               # later packets tighten the bound
            t0 = mon.stamp(lo)
        finally:
            mon.stop()
        run.blocks = [(end, t) for end, t, _ in mon._blocks]
        return data, t0, lo, logs, recs[-1]

    # 11. every packet stamped: dated by when it plays - exactly, not by
    # when it happened to arrive (which is LEAD + a packet early)
    dev = FakeWasapi()
    data, t0, lo, logs, rec = run(dev)
    err = t0 - dev.play_time(lo)
    assert abs(err) < 2e-6, f"stamped {err * 1e3:+.4f} ms off the play time"
    want = ((np.arange(lo, lo + data.size) % 1000) / 1000.0 + 0.01)
    assert np.allclose(data, want, atol=1e-4), "slice is not contiguous"
    old = dev.arrival_bound(lo) - dev.play_time(lo)
    assert old < -0.02, f"arrival stamping should read early here ({old * 1e3:+.1f} ms)"
    # every packet's own stamp is believed - except one read so long after
    # it played that its stamp is not plausible, which a stalled CI runner
    # can make happen (a macOS one did); only those may go unstamped
    late = sum(1 for end, t in dev.delivered
               if dev.play_time(end) - t < ac.PLAYOUT_LEAD[0] + 0.01)
    assert rec.hooked and rec.stamped > 50 and rec.unstamped <= late, \
        (rec.hooked, rec.stamped, rec.unstamped, late)
    assert any("dated by when WASAPI plays it" in m for m in logs), logs
    print(f"playout stamps: {err * 1e6:+.2f} us off the play time "
          f"(arrival stamping read {old * 1e3:+.1f} ms)")

    # 12. flagged, bogus and missing stamps among good ones: never early,
    # and the good ones keep the dating tight
    def mixed(k, t):
        if k % 4 == 1:
            return 0, 0                                  # no stamp
        if k % 4 == 2:
            return int(round(t * 1e7)), ac.TIMESTAMP_ERROR
        if k % 4 == 3:
            return int(round((t + 5.0) * 1e7)), 0        # not plausible
        return int(round(t * 1e7)), 0
    dev = FakeWasapi(stamp=mixed, silent=lambda k: k % 13 == 5)
    data, t0, lo, logs, rec = run(dev)
    err = t0 - dev.play_time(lo)
    early = min(t - dev.play_time(end) for end, t in run.blocks)
    assert early > -2e-6, f"a block was dated {early * 1e3:+.3f} ms early"
    assert abs(err) < 2e-6, f"mixed stamps dated {err * 1e3:+.4f} ms off"
    assert rec.stamped and rec.unstamped
    print(f"playout stamps: {err * 1e6:+.2f} us with three in four packets "
          "flagged, bogus or missing; no block early")

    # 13. no usable stamps at all: dated by arrival, exactly as before
    dev = FakeWasapi(stamp=lambda k, t: (0, 0))
    data, t0, lo, logs, rec = run(dev, seconds=1.2)
    err = t0 - dev.arrival_bound(lo)
    assert -0.0005 < err < 0.002, f"fallback dated {err * 1e3:+.3f} ms off arrival"
    assert any("not usable" in m for m in logs), logs

    # 14. a soundcard the hook cannot reach: its own record(), dated by arrival
    def no_hook(rec, note):
        raise AttributeError("no _ppCaptureClient")
    dev = FakeWasapi()
    data, t0, lo, logs, rec = run(dev, hook=no_hook)
    err = t0 - dev.arrival_bound(lo)
    assert not rec.hooked and -0.0005 < err < 0.002, err
    want = ((np.arange(lo, lo + data.size) % 1000) / 1000.0 + 0.01)
    assert np.allclose(data, want, atol=1e-4), "slice is not contiguous"
    assert any("no packet times" in m for m in logs), logs
    print("playout stamps: no stamps, or no hook - dated by arrival as before")


def windows_source_tests():
    """Listen to on Windows opens a speaker's loopback through
    PlayoutRecorder; the host's microphone is opened as before."""
    import sys
    import types

    import audio_capture as ac

    class Dev:
        def __init__(self, name, loopback=False):
            self.name, self.loopback = name, loopback

    speakers = [Dev("Speakers (Realtek(R) Audio)"), Dev("Headphones (Razer Nari)")]
    mics = [Dev("Microphone (Razer Nari)")]
    fake_sc = types.SimpleNamespace(
        all_speakers=lambda: list(speakers), default_speaker=lambda: speakers[0],
        all_microphones=lambda: list(mics), default_microphone=lambda: mics[0],
        get_microphone=lambda name, include_loopback=False: Dev(name, include_loopback))
    saved = sys.modules.get("soundcard"), ac.IS_MAC
    sys.modules["soundcard"] = fake_sc
    ac.IS_MAC = False
    try:
        src, hint = ac._open_source("loopback", "")
        assert isinstance(src, ac.LoopbackSource), src
        assert src.mic.loopback and src.name == "Speakers (Realtek(R) Audio)"
        assert "Realtek" in hint
        src, _ = ac._open_source("loopback", "nari")
        assert isinstance(src, ac.LoopbackSource) and src.name == "Headphones (Razer Nari)"
        src, _ = ac._open_source("mic", "")
        assert not isinstance(src, ac.LoopbackSource) and src.name.startswith("Microphone")
    finally:
        if saved[0] is not None:
            sys.modules["soundcard"] = saved[0]
        else:
            sys.modules.pop("soundcard", None)
        ac.IS_MAC = saved[1]
    print("listen-to on Windows: loopbacks dated by playout, the mic as before")


if __name__ == "__main__":
    main()
