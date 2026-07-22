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

MAX_STREAM_DELAY = 90.0     # longest stream delay we search for
# Fingerprinting a VOICE_CHUNK-second mic block yields only
# (VOICE_CHUNK - FP_WIN) seconds of words: the analysis window and the
# consecutive-frame delta consume the rest. So even a host recording with
# no gaps at all fills at most this fraction of the voice timeline, and
# the coverage gate has to sit well under it - device-open latency and
# fingerprinting time push the real duty cycle lower still.
VOICE_DUTY = (VOICE_CHUNK - fingerprint.FP_WIN) / VOICE_CHUNK    # 0.875
MIN_VOICE_COVERAGE = 0.6    # must stay clear of VOICE_DUTY
# A host block is only broadcast once its whole VOICE_CHUNK is recorded,
# so voice covering the end of a probe lands this long after the probe does.
VOICE_SETTLE = VOICE_CHUNK + 2.0
RECONNECT_BACKOFF = (1.0, 2.0, 5.0, 10.0, 15.0)   # then the last, forever


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

    def first_utc(self):
        """UTC of the oldest block held, or None when empty."""
        with self.lock:
            return self.blocks[0][0] if self.blocks else None

    def timeline(self, t_from, t_to):
        """(words, base_utc, coverage 0..1) for the requested span.

        Coverage counts only slots the buffer could plausibly have filled -
        it is the filled fraction from the oldest block onward. Time before
        that is voice nobody recorded yet (a viewer 30 s into a session has
        no host audio from 90 s ago); scoring it as dropout would make
        coverage unreachable for the first minutes of every session.
        """
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
        first = 0 if not blocks else max(
            0, int(round((blocks[0][0] - t_from) / FP_HOP)))
        scored = filled[first:]
        cov = float(scored.mean()) if scored.size else 0.0
        return words, t_from, cov


def measure_delay(voice_buf, probe_words, probe_t0):
    """How far behind the live host is this viewer's stream?

    probe_words: fingerprints of loopback audio recorded starting at
    probe_t0 (shared-clock UTC). Correlates against the host's voice
    timeline. Returns delay seconds, or None when inconclusive.

    The reference has to extend past the *end* of the probe, not its
    start: a probe heard with delay D covers host voice up to
    probe_t0 + MEASURE_SECONDS - D, so a window ending at probe_t0 could
    only ever align streams already D >= MEASURE_SECONDS behind. Callers
    must let the voice buffer settle (VOICE_SETTLE) before measuring, or
    the tail of that window has not been broadcast yet.
    """
    oldest = voice_buf.first_utc()
    if oldest is None:
        return None
    t_from = max(probe_t0 - MAX_STREAM_DELAY, oldest)
    ref, base, cov = voice_buf.timeline(
        t_from, probe_t0 + MEASURE_SECONDS + 1.0)
    if cov < MIN_VOICE_COVERAGE or len(ref) < len(probe_words) + 20:
        return None
    lag, ber, median = fingerprint.best_align(ref, probe_words)
    if ber > fingerprint.DELAY_BER or median - ber < fingerprint.DELAY_MARGIN:
        return None
    spoken_at = base + lag * FP_HOP
    delay = probe_t0 - spoken_at
    if not (-1.0 <= delay <= MAX_STREAM_DELAY):   # -1: alignment noise at D~0
        return None
    return float(max(0.0, delay))


# --------------------------------------------------------------------------
# websocket plumbing (sync client, one thread per direction)
# --------------------------------------------------------------------------

class SessionGone(Exception):
    """The relay no longer holds this room - reconnecting cannot help."""


class Link:
    """Small wrapper over websockets.sync with a receive queue."""

    def __init__(self, url):
        from websockets.sync.client import connect
        self.ws = connect(url, max_size=None, open_timeout=15)
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
        self.token = None         # host secret for resuming the room
        self.viewers = 0
        self.stop_flag = threading.Event()
        self.link = None
        self._anchor = None       # listen mode: (pos, utc, playing)
        self._workers = False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.stop_flag.set()
        if self.link:
            try:                  # tear the room down now, no grace period
                self.link.send({"type": "end"})
            except Exception:
                pass
            self.link.close()

    def _say(self, text):
        self.events.put(("session", text))

    def _run(self):
        """Own the relay connection; reconnect until stopped.

        Everything that can only be done once - clock sync, fingerprinting,
        starting the capture threads - happens outside the reconnect loop,
        so a dropped socket costs a few seconds rather than the session.
        """
        try:
            self._say("Syncing clock...")
            self.clock.sync()
            self._say("Fingerprinting your file (one-way hashes only)...")
            words = fingerprint.fingerprint_file(self.video_path)
            duration, _ = audio_matcher.probe(self.video_path)
            chunks = [words[i:i + FP_CHUNK_WORDS]
                      for i in range(0, len(words), FP_CHUNK_WORDS)]

            attempt = 0
            while not self.stop_flag.is_set():
                try:
                    self._connect(chunks, len(words), duration)
                    attempt = 0
                    self._start_workers()
                    self._serve()
                except SessionGone as e:
                    self._say(str(e))
                    break
                except Exception as e:
                    self._say(f"Host session error: {e}")
                    traceback.print_exc()
                if self.stop_flag.is_set():
                    break
                wait = RECONNECT_BACKOFF[min(attempt,
                                             len(RECONNECT_BACKOFF) - 1)]
                attempt += 1
                self._say(f"Relay connection lost - retrying in {wait:.0f}s.")
                if self.stop_flag.wait(wait):
                    break
        except Exception as e:
            self._say(f"Host session error: {e}")
            traceback.print_exc()
        finally:
            # whatever happened, release the mic and let the UI leave the
            # session rather than sit in one that no longer exists
            self.stop_flag.set()
            if self.link:
                self.link.close()

    def _connect(self, chunks, n_words, duration):
        fresh = self.code is None
        self._say("Connecting to relay..." if fresh
                  else f"Reconnecting to session {self.code}...")
        self.link = Link(self.relay_url)
        if fresh:
            self.link.send({"type": "create", "password": self.password,
                            "meta": {"title": self.title,
                                     "duration": duration}})
        else:
            self.link.send({"type": "resume", "code": self.code,
                            "token": self.token})
        msg = self.link.inbox.get(timeout=20)
        if msg.get("type") == "error":
            raise SessionGone(f"Could not rejoin session {self.code}: "
                              f"{msg.get('reason')}. The session has ended.")
        if msg.get("type") not in ("created", "resumed"):
            raise RuntimeError(f"Relay refused: {msg}")

        if fresh:
            self.code = msg["code"]
            self.token = msg.get("token")
            self._say(f"Session live - code {self.code}. Waiting for viewers.")
            # publish the film fingerprint for verification (cached by the
            # relay, so it survives a reconnect and does not need resending)
            self.link.send({"type": "fp_meta", "ck": "fp_meta",
                            "chunks": len(chunks), "words": n_words,
                            "duration": duration, "title": self.title})
            for i, ch in enumerate(chunks):
                self.link.send({"type": "fp_chunk", "ck": f"fp_chunk_{i}",
                                "i": i, "data": _b64(ch)})
        else:
            self._say(f"Reconnected - session {self.code} is live again "
                      f"({msg.get('viewers', '?')} viewer(s)).")

    def _start_workers(self):
        if self._workers:
            return
        self._workers = True
        threading.Thread(target=self._voice_loop, daemon=True).start()
        if self.position_source is None:
            threading.Thread(target=self._listen_loop, daemon=True).start()
        threading.Thread(target=self._state_loop, daemon=True).start()

    def _serve(self):
        """Handle relay traffic until the link drops or we are stopped."""
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
                return

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
            self.stop_flag.wait(STATE_INTERVAL)

    def _voice_loop(self):
        """Fingerprint the host's MIC - their commentary over the film.

        Viewers fingerprint *stream* audio off loopback (BlackHole on
        macOS); these are two different devices and correlating the delay
        depends on keeping them that way.
        """
        while not self.stop_flag.is_set():
            try:
                utc0, perf0 = self.clock.utc(), time.perf_counter()
                samples, sr, t0 = audio_capture.record_mic(
                    VOICE_CHUNK, mic_name=self.mic_name)
                words = fingerprint.fingerprint_samples(samples, sr)
                # t0 is stamped with the device already running, so this
                # dates the block by when audio started flowing rather
                # than by when we asked - the open latency is not silence
                # the viewer will ever hear
                self.link.send({"type": "voice", "t0": utc0 + (t0 - perf0),
                                "data": _b64(words)})
            except matcher.MatchError:
                pass
            except Exception:
                self.stop_flag.wait(2.0)

    def _listen_loop(self):
        """Track the film's position by listening to the host machine."""
        last_pos = None
        while not self.stop_flag.is_set():
            try:
                utc0, perf0 = self.clock.utc(), time.perf_counter()
                samples, sr, t0 = audio_capture.record_loopback(
                    4.0, speaker_name=self.speaker_name)
                # The match belongs to the instant the capture began, ~0.2 s
                # after we asked for it (the device has to open first).
                # Dating it from the request would put every viewer that far
                # ahead of the host, permanently and invisibly.
                utc_start = utc0 + (t0 - perf0)
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
            self.stop_flag.wait(max(0.0, LISTEN_INTERVAL - 4.0))


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
        self._workers = False

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
        """Own the relay connection; reconnect until stopped.

        Verification is done once - the film fingerprint and the offset it
        produced stay valid across a dropped socket, so a blip costs a few
        seconds of drift rather than re-hashing the whole film.
        """
        try:
            self._say("Syncing clock...")
            self.clock.sync()
            threading.Thread(target=self._recv_loop, daemon=True).start()

            attempt = 0
            while not self.stop_flag.is_set():
                try:
                    self._join()
                    attempt = 0
                    if not self._await_session_data():
                        break     # stopped for a reason already reported
                    if not self.verified:
                        self._verify()
                    self._start_workers()
                    while self.link.alive and not self.stop_flag.is_set():
                        self.stop_flag.wait(0.5)
                except matcher.MatchError as e:
                    self._report_failed_verification(e)
                    break
                except Exception as e:
                    self._say(f"Viewer session error: {e}")
                    traceback.print_exc()
                if self.stop_flag.is_set():
                    break
                wait = RECONNECT_BACKOFF[min(attempt,
                                             len(RECONNECT_BACKOFF) - 1)]
                attempt += 1
                self._say(f"Relay connection lost - retrying in {wait:.0f}s.")
                if self.stop_flag.wait(wait):
                    break
        except Exception as e:
            self._say(f"Viewer session error: {e}")
            traceback.print_exc()
        finally:
            self.stop_flag.set()
            if self.link:
                self.link.close()

    def _join(self):
        self._say("Connecting to relay..." if not self.verified
                  else f"Rejoining session {self.code}...")
        self.link = Link(self.relay_url)
        self.link.send({"type": "join", "code": self.code,
                        "password": self.password})

    def _await_session_data(self):
        """Block until the host's film fingerprint has fully arrived.

        Returns False when the session was stopped instead - a bad code, a
        wrong password or a host teardown all set stop_flag after saying
        exactly what went wrong, and blaming a slow fingerprint on top of
        that only misleads.
        """
        self._say("Waiting for session data...")
        deadline = time.time() + 60
        while not self.stop_flag.is_set():
            meta = self._fp_meta
            if meta is not None and len(self._fp_chunks) >= meta["chunks"]:
                return True
            if time.time() >= deadline:
                raise RuntimeError("Session data never arrived - is the "
                                   "host still fingerprinting?")
            time.sleep(0.3)
        return False

    def _verify(self):
        host_words = np.concatenate(
            [self._fp_chunks[i] for i in range(self._fp_meta["chunks"])])
        self._say("Verifying your copy against the host's hashes...")
        self.delta, ber = verify_media(host_words, self.video_path,
                                       self._fp_meta.get("duration"))
        self.verified = True
        self.link.send({"type": "verified", "ok": True, "offset": self.delta})
        self._say(f"Verified (bit error {ber:.2f}, file offset "
                  f"{self.delta:+.2f}s). Following the session.")

    def _report_failed_verification(self, err):
        if self.link:
            try:
                self.link.send({"type": "verified", "ok": False})
            except Exception:
                pass
        self._say(str(err))

    def _start_workers(self):
        if self._workers:
            return
        self._workers = True
        threading.Thread(target=self._measure_loop, daemon=True).start()
        threading.Thread(target=self._follow_loop, daemon=True).start()

    def _recv_loop(self):
        while not self.stop_flag.is_set():
            link = self.link
            if link is None:
                time.sleep(0.2)
                continue
            try:
                msg = link.inbox.get(timeout=1.0)
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
            elif t == "host_gone":
                self._say("The host dropped off - holding your place. "
                          "Playback keeps going on the last known timeline.")
            elif t == "host_back":
                self._say("The host is back.")
            elif t == "_closed":
                pass          # _run owns reconnecting; not a session end

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
            self.stop_flag.wait(1.0)

    def _measure_loop(self):
        """Measure this viewer's own stream delay off loopback audio.

        This records the *stream* (BlackHole on macOS), never a mic - it is
        the host's voice arriving late through the stream that the
        correlation looks for.
        """
        while not self.stop_flag.is_set():
            try:
                utc0, perf0 = self.clock.utc(), time.perf_counter()
                samples, sr, t0 = audio_capture.record_loopback(
                    MEASURE_SECONDS, speaker_name=self.speaker_name)
                probe = fingerprint.fingerprint_samples(samples, sr)
                # the host cannot have broadcast voice covering the end of
                # this probe yet - its block is still being recorded
                if self.stop_flag.wait(VOICE_SETTLE):
                    break
                d = measure_delay(self.voice, probe, utc0 + (t0 - perf0))
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
            self.stop_flag.wait(
                max(1.0, MEASURE_INTERVAL - MEASURE_SECONDS - VOICE_SETTLE))
