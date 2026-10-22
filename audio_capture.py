"""System-audio capture.

Windows: WASAPI loopback via the `soundcard` package - records whatever is
playing on a speaker/headphone device, no setup needed.

macOS 14.2+: "This Mac's sound" - a Core Audio process tap, run by a small
Swift helper (mac/sstap.swift, built into the app as sstap). It records
everything the Mac plays except StreamSync itself, so the film's own sound
never counts, and hands over each block with the time CoreAudio captured
it. Nothing to install or route; macOS asks once for permission to record
the computer's audio. BlackHole or a microphone can still be picked under
Settings > Listen to - on macOS a "speaker name" is then an input-device
name, recorded like a microphone. Nothing falls back to a microphone on
its own: a mic hears the room (another device playing the stream ahead,
say), late by its own processing.

Timing is the point of this module, not just the samples. Sync is
"the stream was at X when this recording started", so an error in *when*
lands one-for-one in the sync. Reading the clock and then opening the
device - what this used to do - stamps every recording with however long
the device took to open, which varies from call to call. AudioMonitor
instead keeps the device open, counts samples, and timestamps each one
from when its block was delivered. A recording is then a slice of a
continuous buffer with a known start time, and a sync can use audio that
was already captured instead of waiting for more.
"""

import collections
import json
import os
import platform
import struct
import subprocess
import sys
import threading
import time

import numpy as np

CAPTURE_SR = 48000  # native rate on virtually all devices
IS_MAC = sys.platform == "darwin"
SILENCE = 1e-4      # peak below this is "nothing is playing"
BLOCK_S = 0.02      # device read size
STAMP_SPAN = 2.0    # seconds of later blocks used to date a sample
GAP_TOLERANCE = 0.25  # a real stall is far longer than scheduling jitter
OPEN_TIMEOUT = 60.0   # a source may take this long to start delivering: macOS
                      # checking a new helper, or its permission prompt
                      # waiting on the user, the first time it records

SYSTEM_AUDIO = "This Mac's sound"   # the tap, as Settings > Listen to names it
TAP_MACOS = (14, 2)                 # Core Audio process taps arrived in 14.2
HELPER = "sstap"
PERMISSION_FIX = ("System Settings > Privacy & Security > Screen & System "
                  "Audio Recording > System Audio Recording Only, and turn on "
                  "StreamSync")


def macos_version():
    """(major, minor) of the running macOS; (0, 0) off macOS."""
    try:
        return tuple(int(p) for p in platform.mac_ver()[0].split(".")[:2])
    except ValueError:
        return (0, 0)


def tap_supported():
    return IS_MAC and macos_version() >= TAP_MACOS


def helper_path():
    """The sstap helper: beside the executable in the built app, or
    mac/build/sstap in a source checkout (sh mac/build_sstap.sh)."""
    places = [os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "mac", "build", HELPER)]
    if getattr(sys, "frozen", False):
        places.insert(0, os.path.join(os.path.dirname(sys.executable), HELPER))
    return next((p for p in places if os.access(p, os.X_OK)), None)


def system_audio_problem():
    """Why this Mac's sound cannot be recorded here, or None if it can."""
    if not IS_MAC:
        return "Recording through an audio tap is macOS-only."
    if not tap_supported():
        return ("Hearing this Mac's sound needs macOS 14.2 or later. On this "
                "macOS, install BlackHole (existential.audio/blackhole), send "
                "the sound to it with a Multi-Output Device, and pick it "
                "under Settings > Listen to.")
    if helper_path() is None:
        return ("StreamSync's recorder for this Mac's sound (sstap) is "
                "missing. Running from source? Build it once with: "
                "sh mac/build_sstap.sh")
    return None


def list_speakers():
    """Names of capturable sources (Windows: speakers; macOS: this Mac's
    sound, then inputs - BlackHole first)."""
    import soundcard as sc
    if IS_MAC:
        names = [m.name for m in sc.all_microphones()]
        names.sort(key=lambda n: 0 if "blackhole" in n.lower() else 1)
        if tap_supported():
            names.insert(0, SYSTEM_AUDIO)
        return names
    return [s.name for s in sc.all_speakers()]


def default_speaker_name():
    import soundcard as sc
    if IS_MAC:
        if not tap_supported():
            for m in sc.all_microphones():
                if "blackhole" in m.name.lower():
                    return m.name
        # never a microphone by default - without a tap this explains why
        return SYSTEM_AUDIO
    return sc.default_speaker().name


def list_microphones():
    """Real input devices (the host's mic for voice fingerprinting)."""
    import soundcard as sc
    return [m.name for m in sc.all_microphones()]


def _input_hint(name):
    if "blackhole" in name.lower():
        return (f"Captured only silence from '{name}'. BlackHole hears only "
                "what is sent to it: make a Multi-Output Device (your "
                "speakers + BlackHole) the sound output - or pick "
                f"{SYSTEM_AUDIO} under Settings > Listen to, which needs no "
                "setup.")
    return f"Captured only silence from '{name}' - is the stream audible there?"


def _open_source(kind, name):
    """(source, silence hint) for a capture source: a soundcard microphone,
    or this Mac's sound. The hint may be a callable, asked when needed.

    kind "loopback" is the stream's audio; "mic" is the host's own voice.
    """
    import soundcard as sc
    if kind == "mic":
        for m in sc.all_microphones():
            if name and name.lower() in m.name.lower():
                return m, None
        return sc.default_microphone(), None
    if IS_MAC:
        if not name or name == SYSTEM_AUDIO:
            if not tap_supported():
                for m in sc.all_microphones():
                    if "blackhole" in m.name.lower():
                        return m, _input_hint(m.name)
            problem = system_audio_problem()
            if problem:
                raise RuntimeError(problem)
            src = SystemAudioSource()
            return src, src.hint
        for m in sc.all_microphones():
            if name.lower() in m.name.lower():
                return m, _input_hint(m.name)
        raise RuntimeError(f"'{name}' is not connected. Pick what StreamSync "
                           "listens to under Settings > Listen to.")
    spk = None
    if name:
        for s in sc.all_speakers():
            if name.lower() in s.name.lower():
                spk = s
                break
    if spk is None:
        spk = sc.default_speaker()
    return (sc.get_microphone(spk.name, include_loopback=True),
            f"Captured only silence from '{spk.name}' - is the stream "
            "audible on that device?")


class SystemAudioSource:
    """This Mac's sound, in the shape soundcard gives a microphone: a name
    and a recorder() context. Its silence hint depends on what the
    recorder has seen, so it is asked when needed, not fixed at open."""

    name = SYSTEM_AUDIO

    def __init__(self, cmd=None):
        self.cmd = cmd
        self.rec = None

    def recorder(self, samplerate=CAPTURE_SR):
        self.rec = SystemAudioRecorder(samplerate, cmd=self.cmd)
        return self.rec

    def hint(self, problem="silence"):
        if problem == "gap":
            return "The recording of this Mac's sound skipped - sync again."
        if self.rec is not None and self.rec.playing:
            return ("StreamSync hears only silence from this Mac, though "
                    "something is playing - macOS may not be letting it "
                    f"record. Allow it in {PERMISSION_FIX}, then sync again.")
        return ("Nothing is playing on this Mac - is the stream paused or "
                "muted? StreamSync follows the stream this Mac plays. (To "
                "follow one playing on another device, pick a microphone "
                "under Settings > Listen to.)")


class SystemAudioRecorder:
    """Runs the sstap helper and reads its records (see mac/sstap.swift).

    A context manager like soundcard's recorders, plus record_stamped(),
    which also says when each block was heard: CoreAudio's capture time
    plus the output device's own reported delay (device + safety offset +
    stream latency), on the time.perf_counter() clock - which is the clock
    CoreAudio's host times count on macOS. A tap hears the sound on its way
    to the speakers, a few ms (on Bluetooth headphones, a few hundred)
    before anyone does, and a sync wants the moment it is heard.

    Blocks come at the output device's rate; at another rate than the
    monitor's they are resampled onto its grid, keeping their timing. A
    new sound output ends the recording (sstap exits): its clock and delay
    are different, and the next sync starts a fresh one.
    """

    def __init__(self, samplerate=CAPTURE_SR, cmd=None):
        self.sr = samplerate
        self.cmd = cmd or [helper_path(), "--exclude-parent"]
        self.proc = None
        self.playing = None       # anything but StreamSync playing, per sstap
        self.latency = 0.0        # seconds from capture to heard
        self.rate = float(samplerate)
        self.hello = None
        self._changed = False     # sstap said the sound output changed
        self._next_t = None       # resampling: when the next sample falls
        self._prev = None         # (time, value) of the last input sample

    def __enter__(self):
        self._launch()
        return self

    def __exit__(self, *exc):
        self.abort()
        return False

    def _launch(self):
        if not self.cmd or not self.cmd[0]:
            raise RuntimeError(system_audio_problem()
                               or "StreamSync's recorder for this Mac's sound is missing.")
        self.proc = subprocess.Popen(
            self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        while True:
            kind, rec = self._read()
            if kind == "json":
                if rec.get("type") == "hello":
                    self._take_hello(rec)
                    return
                self._note(rec)

    def _take_hello(self, h):
        self.hello = h
        tap = h.get("tap") or {}
        self.rate = float(tap.get("aggregate_rate") or tap.get("rate")
                          or self.sr)
        lat = (h.get("output") or {}).get("latency") or {}
        r = float(lat.get("rate") or self.rate)
        frames = sum(int(lat.get(k, 0)) for k in ("device", "safety", "stream"))
        self.latency = frames / r if r > 0 else 0.0

    def abort(self):
        """Stop sstap: closing its stdin is its cue; then make sure."""
        p = self.proc
        if p is None:
            return
        try:
            p.stdin.close()
        except Exception:
            pass
        try:
            p.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=1.0)
        except Exception:
            pass

    # ------------------------------------------------------------ records

    def _exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.proc.stdout.read(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf

    def _read(self):
        """The next record: ("json", dict) or ("audio", (source, host
        time, samples)). The end of sstap's output raises."""
        try:
            magic = self._exact(4)
            if magic == b"SSJ1":
                (n,) = struct.unpack("<I", self._exact(4))
                return "json", json.loads(self._exact(n))
            if magic == b"SSA1":
                source, host, _st, _now, n, _flags = struct.unpack(
                    "<IdddII", self._exact(36))
                return "audio", (source, host,
                                 np.frombuffer(self._exact(4 * n), dtype="<f4"))
        except EOFError:
            if self._changed:
                raise RuntimeError("The sound output changed - StreamSync "
                                   "listens again from the next sync.")
            code = self.proc.poll() if self.proc else None
            raise RuntimeError("The recorder for this Mac's sound stopped"
                               + (f" (exit {code})." if code is not None else "."))
        raise RuntimeError(f"The recorder for this Mac's sound sent something "
                           f"unexpected ({magic!r}).")

    def _note(self, rec):
        kind = rec.get("type")
        if kind == "status" and "playing" in rec:
            self.playing = bool(rec["playing"])
        elif kind == "event" and rec.get("event") == "output-changed":
            self._changed = True
        elif kind == "error":
            raise RuntimeError(rec.get("message")
                               or "The recorder for this Mac's sound failed.")

    def record_stamped(self, numframes=None):
        """(mono float32 block, perf_counter time just after its last
        sample was heard). Blocks come in sstap's size, not numframes."""
        while True:
            kind, rec = self._read()
            if kind == "json":
                self._note(rec)
                continue
            source, host, data = rec
            if source != 0 or data.size == 0:
                continue                  # a --compare source, or nothing
            t0 = host + self.latency
            data = data.astype(np.float32)
            if self.rate != self.sr:
                data, t0 = self._resample(data, t0)
                if data.size == 0:
                    continue
            return data, t0 + data.size / self.sr

    def record(self, numframes=None):
        return self.record_stamped(numframes)[0]

    def _resample(self, x, t_first):
        """x (at self.rate, first sample at t_first) onto self.sr's sample
        grid, continuing the last block's: (samples, time of the first)."""
        step = 1.0 / self.rate
        tin = t_first + np.arange(x.size) * step
        if self._prev is not None and abs(t_first - self._prev[0] - step) < step:
            tin = np.concatenate(([self._prev[0]], tin))
            x = np.concatenate(([self._prev[1]], x))
        else:
            self._next_t = t_first            # first block, or after a gap
        self._prev = (tin[-1], x[-1])
        n = int(np.floor((tin[-1] - self._next_t) * self.sr + 1e-9)) + 1
        if n <= 0:
            return np.zeros(0, np.float32), self._next_t
        tout = self._next_t + np.arange(n) / self.sr
        self._next_t = tout[-1] + 1.0 / self.sr
        return np.interp(tout, tin, x).astype(np.float32), float(tout[0])


class AudioMonitor:
    """A capture device kept open, with every sample dated.

    Each delivered block is stamped with perf_counter() on arrival. A
    sample cannot have been captured later than the block that carried
    it arrived, so every later block gives an upper bound on when a
    sample happened; the tightest of those over the next STAMP_SPAN
    seconds is its time. That bound only ever errs late by the device's
    own (constant) buffering, which a user's nudge offset absorbs -
    unlike the old open-then-record stamp, which erred by a different
    amount every time.

    A recorder that knows better says so: record_stamped() returns each
    block with the time just after its last sample (this Mac's sound uses
    CoreAudio's own capture times), and the same bound then holds exactly.

    `source` is a callable returning (recorder_context, silence_hint);
    tests pass a fake, the app passes nothing and gets soundcard. The hint
    may be a callable, asked at the moment it is needed.
    """

    def __init__(self, kind="loopback", name=None, sr=CAPTURE_SR, keep=90.0,
                 idle_timeout=120.0, source=None):
        self.kind = kind
        self.name = name
        self.sr = sr
        self.keep = keep
        self.idle_timeout = idle_timeout
        self._source = source
        self._blocks = collections.deque()   # (end_frame, t_arrival, samples)
        self._total = 0                      # frames delivered so far
        self._first = 0                      # oldest frame still buffered
        self._cond = threading.Condition()
        self._thread = None
        self._stop = False
        self._error = None
        self._hint = None
        self._rec = None
        self._live_at = None     # monotonic time the first block arrived
        self._last_use = time.monotonic()

    # ------------------------------------------------------- lifecycle

    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        with self._cond:
            if self.running():
                return
            self._stop = False
            self._error = None
            self._blocks.clear()
            self._live_at = None
            self._first = self._total   # frames keep counting, so a mark()
            self._last_use = time.monotonic()  # from before stays valid
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self):
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        abort = getattr(self._rec, "abort", None)
        if abort is not None:       # a helper process: do not wait on it
            try:
                abort()
            except Exception:
                pass
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)

    def _open(self):
        if self._source is not None:
            return self._source()
        mic, hint = _open_source(self.kind, self.name)
        return mic.recorder(samplerate=self.sr), hint

    def _run(self):
        block = max(1, int(round(BLOCK_S * self.sr)))
        try:
            rec_ctx, self._hint = self._open()
            self._rec = rec_ctx
            with rec_ctx as rec:
                stamped = getattr(rec, "record_stamped", None)
                while True:
                    with self._cond:
                        if self._stop:
                            return
                        if time.monotonic() - self._last_use > self.idle_timeout:
                            return
                    if stamped is not None:
                        data, t = stamped(block)
                    else:
                        data = rec.record(numframes=block)
                        t = time.perf_counter()
                    data = np.asarray(data, dtype=np.float32)
                    if data.ndim > 1:
                        data = data.mean(axis=1)
                    if data.size == 0:
                        continue
                    with self._cond:
                        if self._live_at is None:
                            self._live_at = time.monotonic()
                        self._total += data.size
                        self._blocks.append((self._total, t, data))
                        limit = self._total - int(self.keep * self.sr)
                        while self._blocks and \
                                self._blocks[0][0] - self._blocks[0][2].size < limit:
                            end, _, d = self._blocks.popleft()
                            self._first = end
                        self._cond.notify_all()
        except Exception as e:  # device vanished, permission denied, ...
            with self._cond:
                if not self._stop:
                    self._error = e
                self._cond.notify_all()

    def _hint_text(self, default, problem):
        hint = self._hint
        if callable(hint):
            try:
                hint = hint(problem)
            except Exception:
                hint = None
        return hint or default

    # ------------------------------------------------------- reading

    def mark(self):
        """Frame index of the next sample to arrive - a start point for
        capture() that guarantees audio recorded after this moment."""
        if not self.running():
            self.start()
        with self._cond:
            return self._total

    def stamp(self, frame):
        """perf_counter() time at which `frame` was captured."""
        with self._cond:
            return self._stamp(frame)

    def frame_at(self, perf):
        """The frame captured at perf_counter() time `perf` - the inverse
        of stamp(), and valid for times not yet recorded (a caller can ask
        for audio from a moment in the near future and wait for it)."""
        with self._cond:
            ref = max(self._first, self._total - self.sr)
            t = self._stamp(ref)
            if t is None:
                raise RuntimeError("No audio has arrived yet.")
            return ref + int(round((perf - t) * self.sr))

    def last_stamp(self):
        """When the newest sample was captured, or None - where a stalled
        loopback stopped."""
        with self._cond:
            return self._stamp(self._total - 1) if self._total > self._first else None

    def wait_for(self, frame, timeout):
        """Block until `frame` has been recorded; False on timeout/error."""
        deadline = time.monotonic() + timeout
        with self._cond:
            self._last_use = time.monotonic()
            while self._total <= frame:
                if self._error is not None or time.monotonic() > deadline:
                    return False
                self._cond.wait(0.25)
            return True

    def _stamp(self, frame):
        best = None
        horizon = frame + int(STAMP_SPAN * self.sr)
        for end, t, _ in self._blocks:
            if end < frame:
                continue
            if end > horizon and best is not None:
                break
            bound = t - (end - frame) / self.sr
            if best is None or bound < best:
                best = bound
        return best

    def capture(self, seconds, start=None, timeout=None, allow_silence=False):
        """`seconds` of audio as (mono float32, sr, perf_counter at start);
        see capture_span()."""
        return self.capture_span(seconds, start, timeout, allow_silence)[:3]

    def capture_span(self, seconds, start=None, timeout=None,
                     allow_silence=False):
        """(mono float32, sr, perf_counter at start, start frame).

        start=None takes the most recent audio already buffered (waiting
        only if there is not enough yet); start=a mark() takes audio from
        that point on, waiting for it to arrive. Silence raises unless
        allowed: on a loopback it means the stream is not playing, while
        on a microphone it is just a pause between sentences. The start
        frame lets a caller extend the same recording later.

        The default timeout, seconds + 5, counts from when the source is
        delivering: a source still starting gets up to OPEN_TIMEOUT first,
        so a first sync is not lost to macOS checking the helper or to the
        user reading a permission prompt. An explicit timeout is the whole
        wait.
        """
        need = int(round(seconds * self.sr))
        if not self.running():
            self.start()
        strict = timeout is not None
        timeout = seconds + 5.0 if timeout is None else timeout
        called = time.monotonic()
        with self._cond:
            self._last_use = time.monotonic()
            while True:
                if self._error is not None:
                    raise RuntimeError(f"Audio capture failed: {self._error}")
                if start is None:
                    lo = max(self._first, self._total - need)
                else:
                    lo = max(start, self._first)
                if self._total - lo >= need:
                    break
                if strict or (self._live_at is not None and self._live_at <= called):
                    deadline = called + timeout
                elif self._live_at is not None:     # it started during this wait
                    deadline = self._live_at + timeout
                else:                               # still starting
                    deadline = called + max(timeout, OPEN_TIMEOUT)
                left = deadline - time.monotonic()
                if left <= 0:
                    raise RuntimeError(
                        "No audio arrived from the capture device in time.")
                self._cond.wait(min(left, 0.25))
            data = self._slice(lo, lo + need)
            t0 = self._stamp(lo)
            t1 = self._stamp(lo + need - 1)
        if t0 is None:
            raise RuntimeError("Audio capture could not be timed.")
        # Delivery stalls (WASAPI loopback sends nothing while nothing
        # plays) break the sample-count clock: the slice is not
        # continuous audio, so it cannot be matched as such.
        if t1 is not None and (t1 - t0) - (need - 1) / self.sr > GAP_TOLERANCE:
            raise RuntimeError(self._hint_text("The recording had a gap.", "gap"))
        if not allow_silence and float(np.abs(data).max(initial=0.0)) < SILENCE:
            raise RuntimeError(self._hint_text("Captured only silence.", "silence"))
        return data, self.sr, t0, lo

    def _slice(self, lo, hi):
        out = []
        for end, _, d in self._blocks:
            b0 = end - d.size
            if end <= lo or b0 >= hi:
                continue
            out.append(d[max(lo - b0, 0):min(hi - b0, d.size)])
        return np.concatenate(out) if out else np.zeros(0, np.float32)


# One-shot helpers, for callers that want a single recording. They go
# through a monitor too, so their timestamps are sample-accurate.

def record_loopback(seconds, speaker_name=None, sr=CAPTURE_SR):
    """Record `seconds` of the stream's audio.

    Returns (mono float32 samples, sample_rate, perf_counter_at_start).
    """
    mon = AudioMonitor("loopback", speaker_name, sr=sr, keep=seconds + 5)
    try:
        return mon.capture(seconds, start=mon.mark())
    finally:
        mon.stop()


def record_mic(seconds, mic_name=None, sr=CAPTURE_SR):
    """Record from a microphone. Returns (mono float32, sr, perf_t0).

    Unlike record_loopback, silence is fine here - a streamer pausing
    between sentences is normal, not an error.
    """
    mon = AudioMonitor("mic", mic_name, sr=sr, keep=seconds + 5)
    try:
        return mon.capture(seconds, start=mon.mark(), allow_silence=True)
    finally:
        mon.stop()
