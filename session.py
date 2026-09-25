"""Hosted watch sessions: host engine, viewer engine, and the pure helpers
both are built from.

Wire protocol (JSON over a websocket relay - see relay_server.py):
everything transmitted is positions, UTC timestamps, and non-invertible
binary fingerprint words (see fingerprint.py). No audio, no video, no
content of any kind crosses the network.

Timeline model: the host broadcasts state tuples (position, utc, playing).
A viewer renders the host's timeline *delayed* by their personal stream
delay D - evaluating "where was the host D seconds ago" - which makes
pauses and seeks land exactly when the commentary about them arrives.

Precision: every timestamp here is a sample count or a player clock
reading converted onto the shared NTP clock - never "read the clock, then
start recording", which stamped each block with however long the device
took to open. Host voice fingerprints are cut on a fixed UTC grid, so the
viewer's copy of the host's voice timeline has no per-block rounding, and
both the stream delay and the file offset are aligned below the
fingerprint's 250 ms hop (fingerprint.fine_align).
"""

import base64
import json
import math
import queue
import threading
import time
import traceback

import numpy as np

import audio_capture
import audio_matcher
import fingerprint
import matcher
from ntpclock import SharedClock

FP_HOP = fingerprint.FP_HOP
STATE_INTERVAL = 2.0        # host heartbeat
VOICE_CHUNK = 4.0           # host voice words per message (seconds)
LISTEN_SECONDS = 4.0        # host loopback position check (listen mode)
LISTEN_NEAR = 30.0          # listen-mode search half-width once locked
MISSES_FOR_PAUSE = 2        # listen mode: misses in a row that mean paused
MEASURE_INTERVAL = 25.0     # viewer stream-delay measurement
MEASURE_SECONDS = 10.0
MEASURE_LOOKBACK = 90.0     # longest stream delay looked for
MEASURE_EARLY = 1.0         # a near-zero delay can align this far under 0
# The host sends a voice block only once its whole span is recorded, and
# fingerprinting and the relay hop add a little more: the voice a probe
# ends on (and MEASURE_EARLY past it) has reached the viewer this long
# after the probe ends.
VOICE_SETTLE = VOICE_CHUNK + fingerprint.FP_WIN + 1.5
VERIFY_WINDOW = 45.0        # seconds fingerprinted per verification sample
DRIFT_TOLERANCE = 0.35
RECONNECT_FOR = 60.0        # keep retrying a dropped relay this long
FP_CHUNK_WORDS = 16384      # film fingerprint chunk size (32 KB each)


def _b64(words):
    return base64.b64encode(fingerprint.words_to_bytes(words)).decode()


def _unb64(s):
    return fingerprint.words_from_bytes(base64.b64decode(s))


# --------------------------------------------------------------------------
# pure helpers (unit-tested without network or audio devices)
# --------------------------------------------------------------------------

def verify_media(host_words, path, duration=None):
    """Check a local file against the host's film fingerprint.

    Samples VERIFY_WINDOW-second stretches at 15% / 50% / 85% of the local
    file and aligns each against the host fingerprint. All samples must
    match below VERIFY_BER and agree on one constant offset.

    Returns (delta_seconds, worst_ber) where local_pos = host_pos + delta.
    Raises MatchError with a human explanation when verification fails.
    """
    local_dur, _ = audio_matcher.probe(path)
    if duration and abs(local_dur - duration) > 600:
        raise matcher.MatchError(
            f"Your file runs {local_dur/60:.0f} min, the host's runs "
            f"{duration/60:.0f} min - these look like different cuts.")

    deltas, bers = [], []
    for frac in (0.15, 0.50, 0.85):
        t = min(max(0.0, frac * local_dur), local_dur - VERIFY_WINDOW - 1)
        x = audio_matcher.decode_audio(path, t, VERIFY_WINDOW)
        host_t, ber, _median = fingerprint.fine_align(host_words, x,
                                                      audio_matcher.SR)
        if ber > fingerprint.VERIFY_BER:
            raise matcher.MatchError(
                "Verification failed: your file's audio doesn't match the "
                f"host's (bit error {ber:.2f} at {t/60:.0f} min in). "
                "Sync would be wrong, so it won't start.")
        deltas.append(t - host_t)
        bers.append(ber)
    if max(deltas) - min(deltas) > 1.0:
        raise matcher.MatchError(
            "Verification failed: your file matches in places but with "
            "inconsistent offsets - it's likely a different cut of the film.")
    return float(np.median(deltas)), float(max(bers))


class StateTimeline:
    """The host's play state over time; can answer 'where was the host at
    UTC time t' so viewers can render a delayed copy of the timeline."""

    def __init__(self):
        self.states = []          # list of dicts with pos/utc/playing, utc asc
        self.default_delay = 10.0
        self.lock = threading.Lock()

    def add(self, msg):
        with self.lock:
            self.default_delay = float(msg.get("default_delay",
                                               self.default_delay))
            self.states.append({"pos": float(msg["pos"]),
                                "utc": float(msg["utc"]),
                                "playing": bool(msg["playing"])})
            self.states.sort(key=lambda s: s["utc"])
            del self.states[:-600]

    def at(self, t):
        """(position, playing) on the host timeline at UTC time t."""
        with self.lock:
            current = None
            for s in self.states:
                if s["utc"] <= t:
                    current = s
                else:
                    break
            if current is None:
                return None, False
            pos = current["pos"]
            if current["playing"]:
                pos += t - current["utc"]
            return pos, current["playing"]


class VoiceBuffer:
    """Host voice fingerprints on the shared clock, assembled into a
    continuous word timeline for delay correlation."""

    def __init__(self):
        self.blocks = []          # (t0_utc, words)
        self.lock = threading.Lock()

    def add(self, t0, words):
        with self.lock:
            self.blocks.append((float(t0), words))
            self.blocks.sort(key=lambda b: b[0])
            cutoff = self.blocks[-1][0] - 240.0
            self.blocks = [b for b in self.blocks if b[0] >= cutoff]

    def first_utc(self):
        """UTC the oldest block held starts at, or None while empty."""
        with self.lock:
            return self.blocks[0][0] if self.blocks else None

    def timeline(self, t_from, t_to):
        """(words, base_utc, coverage 0..1) for the requested span.

        The base snaps to the FP_HOP grid the host cuts its words on, so a
        block lands on exact word slots rather than rounded ones.
        """
        base = math.floor(t_from / FP_HOP) * FP_HOP
        n = max(0, int(round((t_to - base) / FP_HOP)))
        words = np.zeros(n, dtype=np.uint16)
        filled = np.zeros(n, dtype=bool)
        with self.lock:
            blocks = list(self.blocks)
        for t0, w in blocks:
            i = int(round((t0 - base) / FP_HOP))
            lo, hi = max(i, 0), min(i + len(w), n)
            if lo < hi:
                words[lo:hi] = w[lo - i:hi - i]
                filled[lo:hi] = True
        cov = float(filled.mean()) if n else 0.0
        return words, base, cov


def measure_delay(voice_buf, samples, sr, probe_t0):
    """How far behind the live host is this viewer's stream?

    samples: loopback audio whose first sample was heard at probe_t0
    (shared-clock UTC). Aligned against the host's voice timeline below
    the fingerprint hop. Returns delay seconds, or None when inconclusive.

    A probe heard D late holds host voice up to its own end minus D, and
    the alignment needs all of it inside the reference - so the reference
    runs past the probe's end. One that stopped 5 s after a 10 s probe
    began could only align streams 5 s or more behind. Callers wait
    VOICE_SETTLE after the probe first, or that tail has not arrived.

    The look-back stops at the oldest voice held. A viewer who joined
    30 s ago holds no host voice from 90 s ago - the relay does not
    replay it - and scoring that as a dropout kept every viewer under
    the coverage gate for its first ~80 s.
    """
    first = voice_buf.first_utc()
    if first is None:
        return None
    probe_end = probe_t0 + len(samples) / sr
    ref, base, cov = voice_buf.timeline(
        max(probe_t0 - MEASURE_LOOKBACK, first), probe_end + MEASURE_EARLY)
    probe_words = int(len(samples) / sr / FP_HOP)
    if cov < 0.8 or len(ref) < probe_words + 20:
        return None
    try:
        spoken, ber, median = fingerprint.fine_align(ref, samples, sr)
    except matcher.MatchError:
        return None
    if ber > fingerprint.DELAY_BER or median - ber < fingerprint.DELAY_MARGIN:
        return None
    delay = probe_t0 - (base + spoken)
    # clock sync and device latency put a stream with next to no delay a
    # little either side of 0
    if not (-MEASURE_EARLY <= delay <= MEASURE_LOOKBACK):
        return None
    return float(max(0.0, delay))


class ListenTracker:
    """Listen-mode host position from a stream of matches and misses.

    Kept separate from the audio so the pause logic can be tested. The
    old version marked a pause by relabelling the last good anchor
    "paused" - at that anchor's own, old timestamp - which told every
    viewer the host had stopped where it was up to 14 s earlier: films
    jumped back and paused early, and any quiet passage did the same.
    Here a pause needs MISSES_FOR_PAUSE misses in a row, is placed where
    the audio actually stopped, and a resume is dated to when playback
    restarted rather than when it was noticed.
    """

    def __init__(self):
        self.anchor = None        # (pos, utc, playing) - what hosts send
        self.misses = 0
        self.first_miss_utc = None

    def predicted(self, utc):
        if self.anchor is None:
            return None
        pos, at, playing = self.anchor
        return pos + (utc - at if playing else 0.0)

    def hit(self, pos, utc):
        """The film was at `pos` when a recording starting at `utc` began."""
        if self.anchor is not None and not self.anchor[2]:
            paused_pos, paused_at, _ = self.anchor
            ran = pos - paused_pos
            # resumed from where it paused: date the restart, not the notice
            if -0.5 <= ran <= utc - paused_at:
                self.anchor = (paused_pos, utc - max(ran, 0.0), True)
                self.misses = 0
                return self.anchor
        self.anchor = (pos, utc, True)
        self.misses = 0
        return self.anchor

    def miss(self, utc, stopped_utc=None):
        """A recording starting at `utc` found no film. `stopped_utc` is
        when the audio went silent, if that could be seen."""
        self.misses += 1
        if self.misses == 1:
            self.first_miss_utc = utc
        if (self.anchor is None or not self.anchor[2]
                or self.misses < MISSES_FOR_PAUSE):
            return self.anchor
        when = stopped_utc if stopped_utc is not None else self.first_miss_utc
        when = max(when, self.anchor[1])
        self.anchor = (self.predicted(when), when, False)
        return self.anchor


def silence_start(samples, sr, min_run=1.0):
    """Offset (s) where a run of digital silence of at least `min_run`
    begins, or None - how a paused player shows up on a BlackHole-style
    loopback that keeps delivering zeros."""
    blk = int(0.25 * sr)
    if blk <= 0 or len(samples) < blk:
        return None
    n = len(samples) // blk
    peaks = np.abs(samples[:n * blk].reshape(n, blk)).max(axis=1)
    quiet = peaks < audio_capture.SILENCE
    need = max(1, int(round(min_run / 0.25)))
    run = 0
    for i, q in enumerate(quiet):
        run = run + 1 if q else 0
        if run >= need:
            return (i - need + 1) * 0.25
    return None


# --------------------------------------------------------------------------
# websocket plumbing (sync client, one thread per direction)
# --------------------------------------------------------------------------

class Link:
    """Small wrapper over websockets.sync. Messages land in `inbox`, which
    outlives any one connection so a reconnect keeps the same reader."""

    def __init__(self, url, inbox):
        from websockets.sync.client import connect
        # Entered by hand because this connection outlives any `with`
        # block; websockets 17.1 deprecates a bare connect() and older
        # versions lack its legacy=True alternative.
        self.ws = connect(url, max_size=None, open_timeout=10).__enter__()
        self.inbox = inbox
        self.alive = True
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self.inbox.put(msg)
        except Exception:
            pass
        self.alive = False
        self.inbox.put({"type": "_closed", "link": id(self)})

    def send(self, obj):
        self.ws.send(json.dumps(obj))

    def close(self):
        self.alive = False
        try:
            self.ws.close()
        except Exception:
            pass


def _connect(url, inbox, hello, ok_types, stop, deadline):
    """Open a Link and send `hello`, retrying with backoff until a reply
    in ok_types arrives. Returns (link, reply); raises RuntimeError with
    the relay's reason if it refuses."""
    delay = 1.0
    while not stop.is_set():
        link = None
        try:
            link = Link(url, inbox)
            link.send(hello)
            wait_until = time.monotonic() + 10
            while time.monotonic() < wait_until:
                try:
                    msg = inbox.get(timeout=0.5)
                except queue.Empty:
                    continue
                t = msg.get("type")
                if t in ok_types:
                    return link, msg
                if t == "error":
                    link.close()
                    raise RuntimeError(msg.get("reason") or "refused")
                if t == "_closed" and msg.get("link") == id(link):
                    break
                # anything else predates this connection; drop it
        except RuntimeError:
            raise
        except Exception:
            pass
        if link is not None:
            link.close()
        if time.monotonic() + delay > deadline:
            break
        stop.wait(delay)
        delay = min(delay * 2, 8.0)
    raise RuntimeError("could not reach the relay")


# --------------------------------------------------------------------------
# host engine
# --------------------------------------------------------------------------

class HostSession:
    """Fingerprints the film, opens a room, then broadcasts position hints
    and voice fingerprints. Position comes from a player callback when the
    film plays inside StreamSync, or from loopback listening when it plays
    anywhere else on the host machine.
    """

    def __init__(self, relay_url, video_path, events, password=None,
                 position_source=None, mic_name=None, speaker_name=None,
                 default_delay=10.0, title=None):
        """events: queue receiving ("session", text) tuples for the UI.
        position_source: callable -> (pos_seconds, playing, perf_counter
        when that was true) - or the older (pos, playing) - or None to use
        loopback listening."""
        self.relay_url = relay_url
        self.video_path = video_path
        self.events = events
        self.password = password
        self.position_source = position_source
        self.mic_name = mic_name
        self.speaker_name = speaker_name
        self.default_delay = default_delay
        self.title = title or "session"
        self.clock = SharedClock()
        self.code = None
        self.token = None
        self.viewers = 0
        self.stop_flag = threading.Event()
        self.link = None
        self.inbox = queue.Queue()
        self.tracker = ListenTracker()
        self._monitors = []

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        """User ended it: tell viewers now rather than after the relay's
        reconnect grace period."""
        self.stop_flag.set()
        if self.link:
            try:
                self.link.send({"type": "end"})
            except Exception:
                pass
            self.link.close()
        self._shutdown()

    def _shutdown(self):
        self.stop_flag.set()
        self.clock.stop()
        for m in self._monitors:
            m.stop()

    def _say(self, text):
        self.events.put(("session", text))

    def _send(self, obj):
        try:
            self.link.send(obj)
        except Exception:
            pass     # a dropped link reconnects; the next beat goes out

    def _run(self):
        try:
            self._say("Syncing clock...")
            self.clock.sync()
            self.clock.resync_every()
            self._say("Fingerprinting your file (one-way hashes only)...")
            words = fingerprint.fingerprint_file(self.video_path)
            duration, _ = audio_matcher.probe(self.video_path)

            self._say("Connecting to relay...")
            self.link, msg = _connect(
                self.relay_url, self.inbox,
                {"type": "create", "password": self.password,
                 "meta": {"title": self.title, "duration": duration}},
                ("created",), self.stop_flag, time.monotonic() + 20)
            self.code, self.token = msg["code"], msg.get("token")
            self._say(f"Session live - code {self.code}. Waiting for viewers.")

            # publish the film fingerprint for verification (cached by relay)
            chunks = [words[i:i + FP_CHUNK_WORDS]
                      for i in range(0, len(words), FP_CHUNK_WORDS)]
            self.link.send({"type": "fp_meta", "ck": "fp_meta",
                            "chunks": len(chunks), "words": len(words),
                            "duration": duration, "title": self.title})
            for i, ch in enumerate(chunks):
                self.link.send({"type": "fp_chunk", "ck": f"fp_chunk_{i}",
                                "i": i, "data": _b64(ch)})

            threading.Thread(target=self._voice_loop, daemon=True).start()
            if self.position_source is None:
                threading.Thread(target=self._listen_loop, daemon=True).start()
            threading.Thread(target=self._state_loop, daemon=True).start()

            while not self.stop_flag.is_set():
                try:
                    msg = self.inbox.get(timeout=1.0)
                except queue.Empty:
                    continue
                t = msg.get("type")
                if t == "viewers":
                    self.viewers = int(msg.get("n", 0))
                    self._say(f"Session {self.code} - "
                              f"{self.viewers} viewer(s) connected.")
                elif t == "verified":
                    ok = msg.get("ok")
                    self._say(f"A viewer {'verified their copy' if ok else 'FAILED verification'}"
                              f" ({msg.get('viewers', '?')} connected).")
                elif t == "_closed":
                    if self.stop_flag.is_set():
                        break
                    if self.link and msg.get("link") not in (None, id(self.link)):
                        continue         # an old connection's farewell
                    self._say("Relay connection lost - reconnecting...")
                    try:
                        self.link, _ = _connect(
                            self.relay_url, self.inbox,
                            {"type": "resume", "code": self.code,
                             "token": self.token},
                            ("resumed",), self.stop_flag,
                            time.monotonic() + RECONNECT_FOR)
                    except RuntimeError as e:
                        self._say(f"Relay connection lost ({e}). "
                                  "Session ended.")
                        break
                    self._say(f"Session {self.code} - reconnected.")
        except Exception as e:
            self._say(f"Host session error: {e}")
            traceback.print_exc()
        finally:
            # nothing keeps recording once the session is over - the old
            # threads ran (mic open) until the app quit
            self._shutdown()

    def _current_state(self):
        if self.position_source is not None:
            try:
                got = self.position_source()
            except Exception:
                return None
            pos, playing = got[0], got[1]
            if pos is None:
                return None
            at = self.clock.perf_to_utc(got[2]) if len(got) > 2 else self.clock.utc()
            return pos, at, playing
        return self.tracker.anchor

    def _state_loop(self):
        seq = 0
        while not self.stop_flag.is_set():
            st = self._current_state()
            if st is not None and self.link is not None:
                pos, utc, playing = st
                self._send({"type": "state", "ck": "state", "seq": seq,
                            "pos": pos, "utc": utc, "playing": playing,
                            "default_delay": self.default_delay})
                seq += 1
            self.stop_flag.wait(STATE_INTERVAL)

    def _voice_loop(self):
        """Voice words cut on the UTC grid (word k covers utc k*FP_HOP), so
        a viewer can lay every block onto one exact timeline."""
        mon = audio_capture.AudioMonitor("mic", self.mic_name, keep=30)
        self._monitors.append(mon)
        n = int(round(VOICE_CHUNK / FP_HOP))
        span = n * FP_HOP + fingerprint.FP_WIN
        k = None
        while not self.stop_flag.is_set():
            try:
                if k is None or not mon.running():
                    mon.mark()                       # (re)open the device
                    if not mon.wait_for(0 if k is None else mon.mark(), 5.0):
                        raise RuntimeError("microphone delivered nothing")
                    now = self.clock.perf_to_utc(time.perf_counter())
                    k = math.ceil(now / FP_HOP) + 1
                start = mon.frame_at(self.clock.utc_to_perf(k * FP_HOP))
                samples, sr, _, _ = mon.capture_span(
                    span, start=start, allow_silence=True, timeout=span + 5)
                words = fingerprint.fingerprint_samples(samples, sr)[:n]
                self._send({"type": "voice", "t0": k * FP_HOP,
                            "data": _b64(words)})
                k += n
            except matcher.MatchError:
                k = None
            except Exception:
                k = None
                self.stop_flag.wait(2.0)

    def _listen_loop(self):
        """Track the film's position by listening to the host machine:
        back-to-back recordings, so a pause is seen within one of them."""
        mon = audio_capture.AudioMonitor("loopback", self.speaker_name, keep=60)
        self._monitors.append(mon)
        start = mon.mark()
        need = int(round(LISTEN_SECONDS * mon.sr))
        tries = 0
        silent_from = None        # utc where trailing silence began, if any
        while not self.stop_flag.is_set():
            try:
                samples, sr, t0, _ = mon.capture_span(
                    LISTEN_SECONDS, start=start, allow_silence=True)
                start += need
                utc_start = self.clock.perf_to_utc(t0)
            except RuntimeError:
                # A stalled loopback (WASAPI sends nothing while nothing
                # plays) or a dead device. The pause happened when the
                # audio stopped, not when this wait gave up on it.
                last = mon.last_stamp()
                stopped = None if last is None else self.clock.perf_to_utc(last)
                if silent_from is None:
                    silent_from = stopped
                start = mon.mark()
                self.tracker.miss(stopped or self.clock.utc(), stopped_utc=silent_from)
                continue
            quiet = silence_start(samples, sr)
            if quiet is None:
                silent_from = None
            elif silent_from is None:
                silent_from = utc_start + quiet
            if quiet == 0.0:
                self.tracker.miss(utc_start, stopped_utc=silent_from)
                continue
            tries += 1
            guess = self.tracker.predicted(utc_start)
            wide = guess is None or (self.tracker.misses >= 3 and tries % 3 == 0)
            lo = None if wide else guess - LISTEN_NEAR
            hi = None if wide else guess + LISTEN_NEAR
            try:
                feats = audio_matcher.prep_capture(samples, sr)
                t, score, z = audio_matcher.find_match_audio(
                    self.video_path, feats, lo, hi)
            except matcher.MatchError:
                t, score, z = None, 0.0, 0.0
            if t is not None and z >= audio_matcher.Z_OK and \
                    score >= audio_matcher.SCORE_OK:
                self.tracker.hit(t, utc_start)
            else:
                self.tracker.miss(utc_start, stopped_utc=silent_from)


# --------------------------------------------------------------------------
# viewer engine
# --------------------------------------------------------------------------

class ViewerSession:
    """Joins a room, verifies the local file, then drives the player along
    the host's timeline delayed by this viewer's measured stream delay."""

    def __init__(self, relay_url, code, video_path, player, events,
                 password=None, speaker_name=None):
        self.relay_url = relay_url
        self.code = code
        self.video_path = video_path
        self.player = player
        self.events = events
        self.password = password
        self.speaker_name = speaker_name
        self.clock = SharedClock()
        self.timeline = StateTimeline()
        self.voice = VoiceBuffer()
        self.delta = 0.0          # local file pos = host pos + delta
        self.delay = None         # personal stream delay (None = use default)
        self.offset = 0.0         # the user's nudges: + moves the film ahead
        self.verified = False
        self.stop_flag = threading.Event()
        self.link = None
        self.inbox = queue.Queue()
        self._fp_chunks = {}
        self._fp_meta = None
        self._refused = None      # the relay's reason for turning us away
        self._monitor = None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.stop_flag.set()
        if self.link:
            self.link.close()
        self._shutdown()

    def _shutdown(self):
        self.stop_flag.set()
        self.clock.stop()
        if self._monitor is not None:
            self._monitor.stop()

    def _say(self, text):
        self.events.put(("session", text))

    def effective_delay(self):
        return (self.delay if self.delay is not None
                else self.timeline.default_delay)

    def _join(self, deadline):
        return _connect(self.relay_url, self.inbox,
                        {"type": "join", "code": self.code,
                         "password": self.password},
                        ("joined",), self.stop_flag, deadline)

    def _run(self):
        try:
            self._say("Syncing clock...")
            self.clock.sync()
            self.clock.resync_every()
            self._say("Connecting to relay...")
            try:
                self.link, _ = self._join(time.monotonic() + 20)
            except RuntimeError as e:
                self._say(f"Could not join: {e}")
                return
            threading.Thread(target=self._recv_loop, daemon=True).start()

            # wait until the film fingerprint has fully arrived
            self._say("Waiting for session data...")
            deadline = time.time() + 60
            while time.time() < deadline and not self.stop_flag.is_set():
                if (self._fp_meta is not None
                        and len(self._fp_chunks) == self._fp_meta["chunks"]):
                    break
                time.sleep(0.3)
            else:
                if self.stop_flag.is_set():
                    return    # the reason (refused, ended, ...) was already said
                raise RuntimeError("Session data never arrived - is the "
                                   "host still fingerprinting?")

            host_words = np.concatenate(
                [self._fp_chunks[i] for i in range(self._fp_meta["chunks"])])
            self._say("Verifying your copy against the host's hashes...")
            self.delta, ber = verify_media(host_words, self.video_path,
                                           self._fp_meta.get("duration"))
            self.verified = True
            self._send({"type": "verified", "ok": True, "offset": self.delta})
            self._say(f"Verified (bit error {ber:.2f}, file offset "
                      f"{self.delta:+.2f}s). Following the session.")

            threading.Thread(target=self._measure_loop, daemon=True).start()
            self._follow_loop()
        except matcher.MatchError as e:
            self._send({"type": "verified", "ok": False})
            self._say(str(e))
        except Exception as e:
            self._say(f"Viewer session error: {e}")
            traceback.print_exc()
        finally:
            self._shutdown()

    def _send(self, obj):
        try:
            self.link.send(obj)
        except Exception:
            pass

    def _recv_loop(self):
        while not self.stop_flag.is_set():
            try:
                msg = self.inbox.get(timeout=1.0)
            except queue.Empty:
                continue
            t = msg.get("type")
            try:
                if t == "state":
                    self.timeline.add(msg)
                elif t == "voice":
                    self.voice.add(msg["t0"], _unb64(msg["data"]))
                elif t == "fp_meta":
                    self._fp_meta = msg
                elif t == "fp_chunk":
                    self._fp_chunks[int(msg["i"])] = _unb64(msg["data"])
                elif t == "host_away":
                    self._say("The host's connection dropped - waiting for "
                              "them to come back...")
                elif t == "host_back":
                    self._say("The host is back. Following the session.")
                elif t == "ended":
                    self._say("The host ended the session.")
                    self.stop_flag.set()
                elif t == "_closed":
                    if self.stop_flag.is_set():
                        break
                    if self.link and msg.get("link") not in (None, id(self.link)):
                        continue
                    self._say("Relay connection lost - reconnecting...")
                    try:
                        self.link, _ = self._join(time.monotonic() + RECONNECT_FOR)
                        self._say("Reconnected. Following the session.")
                    except RuntimeError as e:
                        self._say(f"Relay connection lost ({e}).")
                        self.stop_flag.set()
            except (KeyError, TypeError, ValueError):
                pass    # a malformed message is not worth the session

    def _follow_loop(self):
        was_playing = None
        while not self.stop_flag.is_set():
            perf = time.perf_counter()
            t_view = self.clock.perf_to_utc(perf) - self.effective_delay()
            host_pos, playing = self.timeline.at(t_view)
            if host_pos is not None:
                target = host_pos + self.delta + self.offset
                try:
                    if playing:
                        cur = self.player.clock()
                        here = None if cur is None else cur[0] + (perf - cur[1])
                        if (was_playing is not True or here is None
                                or abs(here - target) > DRIFT_TOLERANCE):
                            # target is where the film belongs at `perf`;
                            # the player carries it forward from there
                            self.player.sync_seek(target, perf, 0.0)
                        was_playing = True
                    else:
                        if was_playing is not False:
                            self.player.pause()
                            self.player.seek(target)
                        was_playing = False
                except Exception:
                    pass
            self.stop_flag.wait(1.0)

    def _measure_loop(self):
        self._monitor = mon = audio_capture.AudioMonitor(
            "loopback", self.speaker_name, keep=30)
        while not self.stop_flag.is_set():
            try:
                samples, sr, t0, _ = mon.capture_span(MEASURE_SECONDS,
                                                      start=mon.mark())
                probe_t0 = self.clock.perf_to_utc(t0)
                # the host is still recording the voice this probe ends on
                if self.stop_flag.wait(VOICE_SETTLE):
                    break
                d = measure_delay(self.voice, samples, sr, probe_t0)
                if d is not None:
                    first = self.delay is None
                    self.delay = d if first else 0.7 * self.delay + 0.3 * d
                    self._say(f"Your stream delay: {self.delay:.2f}s "
                              f"({'measured' if first else 'updated'}).")
            except (RuntimeError, matcher.MatchError):
                pass        # silence on loopback - stream muted, keep default
            except Exception:
                pass
            self.stop_flag.wait(max(0.0, MEASURE_INTERVAL - MEASURE_SECONDS
                                    - VOICE_SETTLE))
