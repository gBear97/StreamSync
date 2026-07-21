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
"""

import base64
import json
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
VOICE_CHUNK = 4.0           # host mic recording block
LISTEN_INTERVAL = 10.0      # host loopback position check (listen mode)
MEASURE_INTERVAL = 25.0     # viewer stream-delay measurement
MEASURE_SECONDS = 10.0
VERIFY_WINDOW = 45.0        # seconds fingerprinted per verification sample
DRIFT_TOLERANCE = 0.35
SEEK_LATENCY = 0.25
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
    host_dur = len(host_words) * FP_HOP
    if duration and abs(local_dur - duration) > 600:
        raise matcher.MatchError(
            f"Your file runs {local_dur/60:.0f} min, the host's runs "
            f"{duration/60:.0f} min - these look like different cuts.")

    deltas, bers = [], []
    for frac in (0.15, 0.50, 0.85):
        t = min(max(0.0, frac * local_dur), local_dur - VERIFY_WINDOW - 1)
        probe = fingerprint.fingerprint_file(path, t, t + VERIFY_WINDOW)
        lag, ber, _median = fingerprint.best_align(host_words, probe)
        if ber > fingerprint.VERIFY_BER:
            raise matcher.MatchError(
                "Verification failed: your file's audio doesn't match the "
                f"host's (bit error {ber:.2f} at {t/60:.0f} min in). "
                "Sync would be wrong, so it won't start.")
        deltas.append(t - lag * FP_HOP)
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

    def timeline(self, t_from, t_to):
        """(words, base_utc, coverage 0..1) for the requested span."""
        n = max(0, int(round((t_to - t_from) / FP_HOP)))
        words = np.zeros(n, dtype=np.uint16)
        filled = np.zeros(n, dtype=bool)
        with self.lock:
            blocks = list(self.blocks)
        for t0, w in blocks:
            i = int(round((t0 - t_from) / FP_HOP))
            for k in range(len(w)):
                j = i + k
                if 0 <= j < n:
                    words[j] = w[k]
                    filled[j] = True
        cov = float(filled.mean()) if n else 0.0
        return words, t_from, cov


def measure_delay(voice_buf, probe_words, probe_t0):
    """How far behind the live host is this viewer's stream?

    probe_words: fingerprints of loopback audio recorded starting at
    probe_t0 (shared-clock UTC). Correlates against the host's voice
    timeline. Returns delay seconds, or None when inconclusive.
    """
    ref, base, cov = voice_buf.timeline(probe_t0 - 90.0, probe_t0 + 5.0)
    if cov < 0.8 or len(ref) < len(probe_words) + 20:
        return None
    lag, ber, median = fingerprint.best_align(ref, probe_words)
    if ber > fingerprint.DELAY_BER or median - ber < fingerprint.DELAY_MARGIN:
        return None
    spoken_at = base + lag * FP_HOP
    delay = probe_t0 - spoken_at
    if not (0.0 <= delay <= 90.0):
        return None
    return float(delay)


# --------------------------------------------------------------------------
# websocket plumbing (sync client, one thread per direction)
# --------------------------------------------------------------------------

class Link:
    """Small wrapper over websockets.sync with a receive queue."""

    def __init__(self, url):
        from websockets.sync.client import connect
        self.ws = connect(url, max_size=None)
        self.inbox = queue.Queue()
        self.alive = True
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            for raw in self.ws:
                try:
                    self.inbox.put(json.loads(raw))
                except ValueError:
                    pass
        except Exception:
            pass
        self.alive = False
        self.inbox.put({"type": "_closed"})

    def send(self, obj):
        self.ws.send(json.dumps(obj))

    def close(self):
        self.alive = False
        try:
            self.ws.close()
        except Exception:
            pass


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
        position_source: callable -> (pos_seconds, playing) or None to use
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
        self.viewers = 0
        self.stop_flag = threading.Event()
        self.link = None
        self._anchor = None       # listen mode: (pos, utc, playing)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.stop_flag.set()
        if self.link:
            self.link.close()

    def _say(self, text):
        self.events.put(("session", text))

    def _run(self):
        try:
            self._say("Syncing clock...")
            self.clock.sync()
            self._say("Fingerprinting your file (one-way hashes only)...")
            words = fingerprint.fingerprint_file(self.video_path)
            duration, _ = audio_matcher.probe(self.video_path)

            self._say("Connecting to relay...")
            self.link = Link(self.relay_url)
            self.link.send({"type": "create", "password": self.password,
                            "meta": {"title": self.title,
                                     "duration": duration}})
            msg = self.link.inbox.get(timeout=10)
            if msg.get("type") != "created":
                raise RuntimeError(f"Relay refused: {msg}")
            self.code = msg["code"]
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
                    msg = self.link.inbox.get(timeout=1.0)
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
                    if not self.stop_flag.is_set():
                        self._say("Relay connection lost.")
                    break
        except Exception as e:
            self._say(f"Host session error: {e}")
            traceback.print_exc()

    def _current_state(self):
        if self.position_source is not None:
            try:
                pos, playing = self.position_source()
            except Exception:
                pos, playing = None, False
            if pos is None:
                return None
            return pos, self.clock.utc(), playing
        if self._anchor is None:
            return None
        pos, utc, playing = self._anchor
        return pos, utc, playing

    def _state_loop(self):
        seq = 0
        while not self.stop_flag.is_set():
            st = self._current_state()
            if st is not None:
                pos, utc, playing = st
                try:
                    self.link.send({"type": "state", "ck": "state", "seq": seq,
                                    "pos": pos, "utc": utc, "playing": playing,
                                    "default_delay": self.default_delay})
                    seq += 1
                except Exception:
                    pass
            time.sleep(STATE_INTERVAL)

    def _voice_loop(self):
        while not self.stop_flag.is_set():
            try:
                utc_start = self.clock.utc()
                samples, sr, _ = audio_capture.record_mic(
                    VOICE_CHUNK, mic_name=self.mic_name)
                words = fingerprint.fingerprint_samples(samples, sr)
                self.link.send({"type": "voice", "t0": utc_start,
                                "data": _b64(words)})
            except matcher.MatchError:
                pass
            except Exception:
                time.sleep(2.0)

    def _listen_loop(self):
        """Track the film's position by listening to the host machine."""
        last_pos = None
        while not self.stop_flag.is_set():
            try:
                utc_start = self.clock.utc()
                samples, sr, _ = audio_capture.record_loopback(
                    4.0, speaker_name=self.speaker_name)
                feats = audio_matcher.prep_capture(samples, sr)
                lo = None if last_pos is None else last_pos - 60
                hi = None if last_pos is None else last_pos + 90
                t, score, z = audio_matcher.find_match_audio(
                    self.video_path, feats, lo, hi)
                if (z >= audio_matcher.Z_OK
                        and score >= audio_matcher.SCORE_OK):
                    self._anchor = (t, utc_start, True)
                    last_pos = t
                elif self._anchor is not None:
                    pos, utc, _ = self._anchor
                    self._anchor = (pos, utc, False)   # film likely paused
            except RuntimeError:
                if self._anchor is not None:            # silence = paused
                    pos, utc, _ = self._anchor
                    self._anchor = (pos, utc, False)
            except Exception:
                pass
            time.sleep(max(0.0, LISTEN_INTERVAL - 4.0))


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
        self.nudge = 0.0
        self.verified = False
        self.stop_flag = threading.Event()
        self.link = None
        self._fp_chunks = {}
        self._fp_meta = None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.stop_flag.set()
        if self.link:
            self.link.close()

    def _say(self, text):
        self.events.put(("session", text))

    def effective_delay(self):
        return (self.delay if self.delay is not None
                else self.timeline.default_delay) + self.nudge

    def _run(self):
        try:
            self._say("Syncing clock...")
            self.clock.sync()
            self._say("Connecting to relay...")
            self.link = Link(self.relay_url)
            self.link.send({"type": "join", "code": self.code,
                            "password": self.password})
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
                raise RuntimeError("Session data never arrived - is the "
                                   "host still fingerprinting?")

            host_words = np.concatenate(
                [self._fp_chunks[i] for i in range(self._fp_meta["chunks"])])
            self._say("Verifying your copy against the host's hashes...")
            self.delta, ber = verify_media(host_words, self.video_path,
                                           self._fp_meta.get("duration"))
            self.verified = True
            self.link.send({"type": "verified", "ok": True,
                            "offset": self.delta})
            self._say(f"Verified (bit error {ber:.2f}, file offset "
                      f"{self.delta:+.2f}s). Following the session.")

            threading.Thread(target=self._measure_loop, daemon=True).start()
            self._follow_loop()
        except matcher.MatchError as e:
            if self.link:
                try:
                    self.link.send({"type": "verified", "ok": False})
                except Exception:
                    pass
            self._say(str(e))
        except Exception as e:
            self._say(f"Viewer session error: {e}")
            traceback.print_exc()

    def _recv_loop(self):
        while not self.stop_flag.is_set():
            try:
                msg = self.link.inbox.get(timeout=1.0)
            except queue.Empty:
                continue
            t = msg.get("type")
            if t == "state":
                self.timeline.add(msg)
            elif t == "voice":
                self.voice.add(msg["t0"], _unb64(msg["data"]))
            elif t == "fp_meta":
                self._fp_meta = msg
            elif t == "fp_chunk":
                self._fp_chunks[int(msg["i"])] = _unb64(msg["data"])
            elif t == "error":
                self._say(f"Could not join: {msg.get('reason')}")
                self.stop_flag.set()
            elif t == "ended":
                self._say("The host ended the session.")
                self.stop_flag.set()
            elif t == "_closed":
                if not self.stop_flag.is_set():
                    self._say("Relay connection lost.")
                self.stop_flag.set()

    def _follow_loop(self):
        was_playing = None
        while not self.stop_flag.is_set():
            t_view = self.clock.utc() - self.effective_delay()
            host_pos, playing = self.timeline.at(t_view)
            if host_pos is not None:
                target = host_pos + self.delta
                try:
                    if playing:
                        cur = self.player.time()
                        if (was_playing is not True or cur is None
                                or abs(cur - target) > DRIFT_TOLERANCE):
                            self.player.ensure_playing()
                            self.player.seek(target + SEEK_LATENCY)
                        was_playing = True
                    else:
                        if was_playing is not False:
                            self.player.pause()
                            self.player.seek(target)
                        was_playing = False
                except Exception:
                    pass
            time.sleep(1.0)

    def _measure_loop(self):
        while not self.stop_flag.is_set():
            try:
                utc_start = self.clock.utc()
                samples, sr, _ = audio_capture.record_loopback(
                    MEASURE_SECONDS, speaker_name=self.speaker_name)
                probe = fingerprint.fingerprint_samples(samples, sr)
                d = measure_delay(self.voice, probe, utc_start)
                if d is not None:
                    first = self.delay is None
                    self.delay = d if first else 0.7 * self.delay + 0.3 * d
                    self._say(f"Your stream delay: {self.delay:.1f}s "
                              f"({'measured' if first else 'updated'}).")
            except RuntimeError:
                pass        # silence on loopback - stream muted, keep default
            except matcher.MatchError:
                pass
            except Exception:
                pass
            time.sleep(MEASURE_INTERVAL - MEASURE_SECONDS)
