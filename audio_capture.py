"""System-audio capture.

Windows: WASAPI loopback via the `soundcard` package - records whatever is
playing on a speaker/headphone device, no setup needed.

macOS: there is no OS loopback API, so the stream's audio is picked up
through the BlackHole virtual audio device. The user routes sound to a
Multi-Output Device (real speakers + BlackHole) and we record from
BlackHole like a normal microphone. All the same functions apply; a
"speaker name" on macOS is really an input-device name.

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


def list_speakers():
    """Names of capturable sources (Windows: speakers; macOS: inputs)."""
    import soundcard as sc
    if IS_MAC:
        names = [m.name for m in sc.all_microphones()]
        # BlackHole is the loopback carrier on macOS - surface it first
        names.sort(key=lambda n: 0 if "blackhole" in n.lower() else 1)
        return names
    return [s.name for s in sc.all_speakers()]


def default_speaker_name():
    import soundcard as sc
    if IS_MAC:
        for name in list_speakers():
            if "blackhole" in name.lower():
                return name
        try:
            return sc.default_microphone().name
        except Exception:
            return ""
    return sc.default_speaker().name


def list_microphones():
    """Real input devices (the host's mic for voice fingerprinting)."""
    import soundcard as sc
    return [m.name for m in sc.all_microphones()]


def _pick_mac_source(sc, speaker_name):
    mics = sc.all_microphones()
    if speaker_name:
        for m in mics:
            if speaker_name.lower() in m.name.lower():
                return m
    for m in mics:
        if "blackhole" in m.name.lower():
            return m
    return sc.default_microphone()


def _open_source(kind, name):
    """(soundcard microphone object, silence hint) for a capture source.

    kind "loopback" is the stream's audio; "mic" is the host's own voice.
    """
    import soundcard as sc
    if kind == "mic":
        for m in sc.all_microphones():
            if name and name.lower() in m.name.lower():
                return m, None
        return sc.default_microphone(), None
    if IS_MAC:
        mic = _pick_mac_source(sc, name)
        return mic, (
            f"Captured only silence from '{mic.name}'. On macOS the stream "
            "must be routed through BlackHole: install BlackHole, create a "
            "Multi-Output Device (your speakers + BlackHole) in Audio MIDI "
            "Setup, select it as the system output, and pick BlackHole "
            "under 'Listen on'.")
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

    `source` is a callable returning (recorder_context, silence_hint);
    tests pass a fake, the app passes nothing and gets soundcard.
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
            self._first = self._total   # frames keep counting, so a mark()
            self._last_use = time.monotonic()  # from before stays valid
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self):
        with self._cond:
            self._stop = True
            self._cond.notify_all()
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
            with rec_ctx as rec:
                while True:
                    with self._cond:
                        if self._stop:
                            return
                        if time.monotonic() - self._last_use > self.idle_timeout:
                            return
                    data = rec.record(numframes=block)
                    t = time.perf_counter()
                    data = np.asarray(data, dtype=np.float32)
                    if data.ndim > 1:
                        data = data.mean(axis=1)
                    if data.size == 0:
                        continue
                    with self._cond:
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
                self._error = e
                self._cond.notify_all()

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
        """
        need = int(round(seconds * self.sr))
        if not self.running():
            self.start()
        timeout = seconds + 5.0 if timeout is None else timeout
        deadline = time.monotonic() + timeout
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
            raise RuntimeError(self._hint or "The recording had a gap.")
        if not allow_silence and float(np.abs(data).max(initial=0.0)) < SILENCE:
            raise RuntimeError(self._hint or "Captured only silence.")
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
