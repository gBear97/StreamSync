"""What StreamSync does, independent of how it is drawn.

The Windows and macOS shells each used to carry their own copy of
everything here - the sync workers, the auto re-sync loop, sessions,
settings - nearly line for line. Every engine fix had to be made twice,
some were only made once, and none of it could be tested without a
window. The shell (the ui package, one for both platforms) owns widgets,
menus and window juggling; this owns the sync, and tests drive it with a
fake player and fake audio.

It talks to the shell only through the event queue, with
("kind", payload...) tuples the shell polls for:

    ("status", text)     one line for the status bar
    ("session", text)    watch-party status
    ("swap", show, why)  bring the stream window forward (True) or back;
                         why is the reason, for the swap's line in the log
    ("busy_off",)        a manual sync finished; re-enable its buttons
    ("auto_off", text)   auto mode gave up and switched itself off; untick
                         it as a manual uncheck would, and show `text`
    ("synced", info)     a match was decided: a manual sync's result, an
                         auto check that heard the stream, or a resume.
                         info is {"match": film seconds matched, "at":
                         perf_counter when that audio was heard, "trusted":
                         the match passed the matcher's gates, "applied":
                         the film was moved to it, "source": "sync",
                         "auto" or "resume"}. The stream is at
                         match + (perf_counter() - at) now.
"""

import json
import math
import sys
import threading
import time
import traceback
from pathlib import Path

import numpy as np

import audio_capture
import audio_matcher
import diagnostics
import hosting
import matcher
import picture_timing
import session
from players import ExternalPlayer, VLCError

CONFIG_PATH = Path.home() / ".streamsync.json"
SEARCH_WINDOW = 120.0      # a hinted sync searches this far either side
AUDIO_SYNC_SECONDS = 6.0   # manual sync recording length...
AUDIO_RETRY_SECONDS = (12.0, 18.0)  # ...extended this far when it is weak
REFINE_TRIES = 3           # a sync its recording could not make exact (a quiet scene)
REFINE_LISTEN = 10.0       # ...listens again this long, up to REFINE_TRIES times,
REFINE_FIX = 0.05          # ...and moves the film by what that finds within this (a
                           # match is out by ~10-20 ms); more is the stream itself having
REFINE_SAME = 0.005        # moved, so it waits for a second look that agrees this well
AUTO_RECORD_SECONDS = 4.0  # auto-mode recording length
AUTO_RETRY_SECONDS = 8.0   # one longer look before counting a failure
AUTO_FAIL_GIVEUP = 3       # an auto check failing the same way this many
                           # times running is not a blip: stop, and say so
DRIFT_TOLERANCE = 0.35
PAUSE_LOOK_BACK = 25.0     # resume search: behind the pause point...
PAUSE_LOOK_AHEAD = 40.0    # ...and ahead of it, plus time since the pause
PAUSE_GROW_STEP = 60.0     # ...counted in whole steps (one decode per step)
PAUSE_GROW_MAX = 600.0     # ...and at most this much
SESSION_CLOSE_WAIT = 1.5   # how long quitting waits for a session to end
# Picture timing (picture_timing.py): the stream's picture against its sound,
# and the film window's own picture against the player's clock.
MEASURE_FIND = 3.0         # the stream's whole window grabbed first, to find the video
                           # (and on a step at a time while nothing in it moves);
MEASURE_SECONDS = 20.0     # ...then a band through the video, until this long all told
MEASURE_MAX = 60.0         # ...and on, a step at a time, until MEASURE_CUTS clear
MEASURE_CUTS = 5           # cuts are seen or this is reached: a film of long takes
MEASURE_STEP = 5.0         # can go 20 s with only two
GRAB_WIDTH = 160           # grabs shrunk to about this wide
LEAD_SECONDS = 8.0         # grabbing the film's own window after a sync,
LEAD_WIDTH = 320           # ...finer: neighbouring frames of a slow scene are close
LEAD_SETTLE = 25.0         # waiting for a sync's corrections to finish first
LEAD_TRIES = 3             # looks before giving up for this sync: a still or dark
LEAD_RETRY = 10.0          # ...scene names too few frames; s between them
LEAD_RANGE = (-0.05, 0.2)  # a film window lead outside this is not believed
IS_MAC = sys.platform == "darwin"


def fmt_time(s):
    # whole tenths, so rounding carries: rounding only the fraction of
    # 119.96 gave the impossible "1:60.0"
    tenths = int(round(max(0.0, float(s)) * 10))
    whole, frac = divmod(tenths, 10)
    h, rem = divmod(whole, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}.{frac}"
    return f"{m}:{sec:02d}.{frac}"


def parse_time(text):
    """'1:23:45', '23:45', '95' -> seconds; empty -> None."""
    text = text.strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3 or not all(p.strip() for p in parts):
        raise ValueError(f"Cannot parse time '{text}' (use h:mm:ss, m:ss or seconds)")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def trusted(score, z):
    return score >= audio_matcher.SCORE_OK and z >= audio_matcher.Z_OK


def playing_position_at(player, perf):
    """Where `player` was at perf_counter() time `perf`, if it is playing.

    players.clock() pairs a position with the instant it was true, so
    this is exact to the player's own clock rather than off by however
    long ago time() happened to be read.
    """
    clk = player.clock()
    if clk is None:
        return None
    pos, at = clk
    return pos + (perf - at)


class SyncController:
    """The sync engine's orchestration. One per app window."""

    def __init__(self, events, embedded, monitor_factory=None):
        self.q = events
        self.embedded = embedded
        self.external = None
        self.player = embedded
        self.video_path = None
        self._embedded_path = None   # the film the built-in player holds
        self.offset = 0.0            # user's accumulated nudge, seconds
        # Where the film goes against the stream (placement_offset): on its
        # "sound", or on its "picture", which can run behind the sound
        # (picture_lags, per stream: {key: {"lag", "when", "cuts"}}). The
        # film window shows a frame film_lead after the player's clock is
        # there (None: not measured yet), so the clock runs that far ahead.
        self.line_up = "sound"
        self.picture_lags = {}
        self.stream_key = ""         # the stream playing (picture_timing.channel_key)
        self.film_lead = None
        # The platform's screen grabbing, or None where there is none:
        # grab(rect, seconds, width) -> (times, images), and
        # film_picture_rect() -> (x, y, w, h) of the film's picture on
        # screen, or None while it is not showing. Both run on threads.
        self.grab = None
        self.film_picture_rect = None
        self._measuring = False
        self.audio_device = ""       # substring of the capture device name
        self.auto_enabled = False
        self.auto_follow = True
        self.auto_interval = 30
        self.relay_url = "ws://localhost:8765"
        self.host_via = "tunnel"     # "tunnel": host through this computer; "relay": relay_url
        self.position_from = "listen"  # a host's film position: "listen" (heard) or "player"
        self.hosting = None          # hosting.Hosting while hosting through this computer
        self.public_relay = None     # the address viewers use then (wss://….trycloudflare.com)
        self._ended_hosting = []     # Hostings stopped or stopping, for close() to make sure of
        self.session = None          # active HostSession / ViewerSession
        self._closers = []           # threads stopping sessions since ended
        self._monitor_factory = monitor_factory or (
            lambda name: audio_capture.AudioMonitor("loopback", name))
        self._monitor = None
        self._monitor_name = None
        self._lock = threading.Lock()
        self._busy = False
        self._gen = 0                # bumped by every manual sync
        self._closing = False
        self._auto_thread = None
        self._now = time.monotonic   # tests substitute a simulated clock

    def start(self):
        self._auto_thread = threading.Thread(target=self._auto_loop, daemon=True)
        self._auto_thread.start()

    # -------------------------------------------------------------- state

    @property
    def busy(self):
        return self._busy

    def _say(self, text):
        self.q.put(("status", text))

    def session_running(self):
        return self.session is not None and not self.session.stop_flag.is_set()

    def _loopback(self):
        """The shared, always-dated stream-audio monitor for the current
        device. Kept open between syncs so a sync can use audio already
        heard; it closes itself after two idle minutes."""
        with self._lock:
            if self._monitor is None or self._monitor_name != self.audio_device:
                if self._monitor is not None:
                    self._monitor.stop()
                self._monitor = self._monitor_factory(self.audio_device)
                self._monitor_name = self.audio_device
            return self._monitor

    # ------------------------------------------------------------ players

    def load_file(self, path):
        """Load `path` into the active player. VLCError propagates for the
        embedded player; external loads report through the queue."""
        if self.player is self.embedded:
            self.embedded.load(path)
            # Named only once it holds: a load that raised used to leave
            # video_path naming a film nothing held, so the shell showed it
            # as open and Sync failed with "No video file loaded.".
            self.video_path = path
            self._embedded_path = path
            return
        self.video_path = path
        name = Path(path).name

        def spawn():
            try:
                self.external.load(path)
                self._say(f"Loaded {name} in external VLC.")
            except Exception as e:
                self._say(f"External VLC: {e}")
        threading.Thread(target=spawn, daemon=True).start()

    def use_external(self, mute):
        """Switch playback to the real VLC app. Returns an error message,
        or None on success."""
        try:
            if self.external is None:
                self.external = ExternalPlayer()
        except VLCError as e:
            return str(e)
        self.embedded.pause()
        self.player = self.external
        if self.video_path:
            t = self.embedded.time()
            path = self.video_path

            def spawn():
                try:
                    self.external.load(path)
                    if t:
                        self.external.seek(t)
                    self.external.set_mute(mute)
                    self._say("Loaded in external VLC - use Sync/Resync to "
                              "line it up. Use VLC's own menus for subtitles.")
                except Exception as e:
                    self._say(f"External VLC: {e}")
            self._say("Starting external VLC...")
            threading.Thread(target=spawn, daemon=True).start()
        return None

    def use_embedded(self):
        """Switch playback back to the built-in player. VLCError
        propagates, as from load_file.

        A film opened while external VLC had playback went to VLC alone,
        so it is loaded here: the built-in player would otherwise play
        nothing, or the film it had before, and Sync would fail with "No
        video file loaded.". A film it already holds is not reloaded, so
        it keeps its place and its subtitles. A new one waits at its start
        for the first sync, as it does when opened with this player active
        (libvlc ignores a seek before playback starts, so VLC's position
        cannot be carried over the way use_external carries this one's)."""
        if self.external is not None:
            self.external.pause()
        self.player = self.embedded
        if self.video_path and self._embedded_path != self.video_path:
            self.embedded.load(self.video_path)
            self._embedded_path = self.video_path

    def set_mute(self, mute):
        self.player.set_mute(mute)

    def nudge(self, delta):
        """Shift the picture by `delta` seconds and remember it.

        In a watch party the session owns the playhead and would undo a
        nudge within a second, so the nudge goes to the session instead.
        """
        self.player.nudge(delta)
        if isinstance(self.session, session.ViewerSession) and self.session_running():
            self.session.offset += delta
            return self.session.offset
        self.offset += delta
        return self.offset

    def reset_offset(self):
        self.offset = 0.0
        if isinstance(self.session, session.ViewerSession):
            self.session.offset = 0.0

    # ----------------------------------------------------------- placement

    def stream_lag(self):
        """How far the current stream's picture runs behind its sound, as
        measured (0.0 if it has not been)."""
        rec = self.picture_lags.get(self.stream_key)
        return float(rec["lag"]) if rec else 0.0

    def placement_offset(self):
        """Where the film belongs against the stream's sound, in seconds of
        film: the user's nudge, plus the film window's lead (its picture
        shows a frame that long after the clock reaches it, so the clock
        runs ahead), less the stream's picture lag when lining up with the
        stream's picture. Every sync, check and resume places the film by
        this."""
        off = self.offset + (self.film_lead or 0.0)
        if self.line_up == "picture":
            off -= self.stream_lag()
        return off

    def set_line_up(self, mode):
        """Line the film up with the stream's "sound" or its "picture", and
        move it there now."""
        before = self.placement_offset()
        self.line_up = "picture" if mode == "picture" else "sound"
        self._shift(self.placement_offset() - before)

    def set_stream_key(self, key):
        """The stream playing now: its measured picture lag applies."""
        key = key or ""
        if key == self.stream_key:
            return
        before = self.placement_offset()
        self.stream_key = key
        self._shift(self.placement_offset() - before)

    def _shift(self, delta):
        """Move the film by delta seconds, as a nudge does, when a setting
        changes where it belongs. Not in a watch party - the session places
        the film there - and not before there is a film."""
        if abs(delta) < 0.0005 or not self.video_path or self.session_running():
            return
        try:
            self.player.nudge(delta)
        except Exception as e:
            diagnostics.log(f"picture: could not move the film {delta * 1000:+.0f} ms: {e}")

    # --------------------------------------------------------- manual sync

    def _claim(self):
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._gen += 1
            return True

    def _release(self):
        with self._lock:
            self._busy = False
        self.q.put(("busy_off",))

    def sync(self, hint_text, mute=True):
        """Search around the hint (or the whole film). Raises ValueError
        for an unparseable hint; returns False if a sync is already
        running."""
        center = parse_time(hint_text)
        if center is None:
            return self.search(None, None, mute)
        return self.search(center - SEARCH_WINDOW, center + SEARCH_WINDOW,
                           mute)

    def resync(self, hint_text, mute=True):
        """Search around where the film is now - mostly backwards, since a
        paused stream leaves the local copy ahead."""
        t = self.player.time()
        if t is None:
            return self.sync(hint_text, mute)
        here = t - self.placement_offset()      # where the stream should be
        return self.search(here - SEARCH_WINDOW, here + 30.0, mute)

    def search(self, a, b, mute=True):
        """Start a sync of the stretch a..b (None for the whole film) in
        the background."""
        if not self._claim():
            return False
        args = (a, b, self.player, self.placement_offset(), mute)
        threading.Thread(target=self._audio_worker, args=args,
                         daemon=True).start()
        return True

    def _first_sync(self, player):
        """Nothing to lose: the film has never started, so even a weak best
        guess beats staying at 0:00. Once it has a position, a weak match
        must not throw it somewhere random."""
        try:
            return player.time() is None
        except Exception:
            return True

    def _audio_worker(self, a, b, player, offset, mute):
        try:
            first = self._first_sync(player)
            mon = self._loopback()
            if not mon.running():
                self._say(f"Listening to the stream ({AUDIO_SYNC_SECONDS:.0f} s)...")
            start = None
            for seconds in (AUDIO_SYNC_SECONDS,) + AUDIO_RETRY_SECONDS:
                if start is not None:
                    self._say(f"Weak match - listening longer ({seconds:.0f} s)...")
                # the first look uses audio already heard - usually no wait
                # at all; longer looks extend that same recording
                samples, sr, t0, start = mon.capture_span(seconds, start=start)
                feats = audio_matcher.prep_capture(samples, sr)
                match_t, score, z = audio_matcher.find_match_audio(
                    self.video_path, feats, a, b, progress=self._say)
                ok = trusted(score, z)
                self._log("sync", "audio", a, b, match_t, score, z, seconds,
                          applied=ok or first, offset=offset)
                if ok:
                    break
            apply = ok or first
            exact = False
            if ok:
                match_t, exact = self._refine(samples, sr, match_t, "sync")
            if apply:
                self._say("Seeking...")
                player.sync_seek(match_t, t0, offset,
                                 waiting=lambda: self._say(self._opening_hint()))
                player.set_mute(mute)
                self.q.put(("swap", False, "a sync moved the film"))
                self._after_sync(self._gen, refine=ok and not exact)
            msg = (f"Matched stream audio at {fmt_time(match_t)} "
                   f"(score {score:.1f}, peak z {z:.0f}).")
            if ok:
                msg += " Nudge if the picture leads/lags the voice track."
            elif apply:
                msg += (" Weak match - used as a first guess; " + self._weak_hint())
            else:
                msg += (" Weak match - NOT applied, playback left where it "
                        "was. " + self._weak_hint())
            self._say(msg)
            self.q.put(("synced", {"match": match_t, "at": t0, "trusted": ok,
                                   "applied": apply, "source": "sync"}))
        except Exception as e:
            self._say(f"Sync failed: {e}")
        finally:
            self._release()

    @staticmethod
    def _opening_hint():
        """Said when the film is slow to start on a sync. A first play
        waits on its file's open, and on a Mac that can be macOS asking
        whether StreamSync may use the film's folder - a prompt that held
        one sync 58 s."""
        if IS_MAC:
            return ("Opening the film... If macOS is asking whether StreamSync "
                    "may open the film's folder, allow it.")
        return "Opening the film... (is its drive awake and connected?)"

    @staticmethod
    def _weak_hint():
        if IS_MAC:
            return ("Check the stream is playing on this Mac with its sound "
                    "on, or try a louder scene.")
        return ("The commentary may be drowning the film audio; try a "
                "louder scene.")

    # ----------------------------------------------------------- auto mode

    def set_auto(self, enabled, follow, interval):
        self.auto_enabled = bool(enabled)
        self.auto_follow = bool(follow)
        try:
            self.auto_interval = max(10, int(interval))
        except (TypeError, ValueError):
            self.auto_interval = 30
        if self.auto_enabled:
            self._say(f"Auto re-sync on: checking every {self.auto_interval} s"
                      + (", following pauses." if self.auto_follow else "."))

    def _auto_probe(self, lo, hi):
        """One listen+match attempt: (t, score, z, t0) when confident, else
        None. A weak first look gets one longer look before it counts as a
        miss, so a quiet line of dialogue is not mistaken for a pause.
        Silence counts as a miss: a silent stream is a paused stream as far
        as syncing is concerned. A film that cannot be read does not: its
        errors propagate, or a vanished film would pass for a paused
        stream and be waited on forever."""
        mon = self._loopback()
        start = mon.mark()
        for seconds in (AUTO_RECORD_SECONDS, AUTO_RETRY_SECONDS):
            try:
                samples, sr, t0 = mon.capture(seconds, start=start)
                feats = audio_matcher.prep_capture(samples, sr)
            except RuntimeError:    # heard nothing usable: a miss
                return None
            t, score, z = audio_matcher.find_match_audio(
                self.video_path, feats, lo, hi)
            if trusted(score, z):
                return t, score, z, t0
        return None

    def auto_step(self, state):
        """One pass of auto mode; returns seconds until the next pass.

        `state` carries mode ("normal" or "probe"), failures, pause_point,
        paused_at and film (what auto mode paused) across passes; held
        marks a pause kept while auto mode stood aside. Split out of the
        loop so tests can drive it without threads or sleeps.
        """
        player = self.player
        gen = self._gen
        if state["mode"] == "normal":
            if not player.is_playing():
                return 5
            clk = player.clock()
            if clk is None:
                return 5
            off = self.placement_offset()
            here = clk[0] - off               # where the stream should be
            hit = self._auto_probe(here - 45, here + 50)
            if hit and not self._stale(gen):
                t, score, z, t0 = hit
                state["failures"] = 0
                # the player is meant to sit at stream + offset
                off = self.placement_offset()
                pos_t0 = playing_position_at(player, t0)
                drift = None if pos_t0 is None else (t + off) - pos_t0
                self._log("auto", "audio", here - 45, here + 50, t, score, z,
                          AUTO_RECORD_SECONDS, drift=drift, offset=off,
                          applied=drift is not None and abs(drift) > DRIFT_TOLERANCE)
                if drift is not None and abs(drift) > DRIFT_TOLERANCE:
                    player.sync_seek(t, t0, off)
                    self._say(f"Auto: corrected {drift:+.2f}s drift "
                              f"(score {score:.1f}, z {z:.0f}).")
                if drift is not None:
                    # heard and checked: the film is in sync now, moved or
                    # not. (drift is None when the player's position at that
                    # moment could not be read - it paused, or its clock had
                    # not settled - so nothing was checked.)
                    self.q.put(("synced", {"match": t, "at": t0, "trusted": True,
                                           "applied": abs(drift) > DRIFT_TOLERANCE,
                                           "source": "auto"}))
                return self.auto_interval
            if hit:
                return 1     # a manual sync or a session overtook this one
            state["failures"] += 1
            if state["failures"] == 1:
                # the stream stopped somewhere after the last good match and
                # before this look - the player has run on since, so this is
                # the better guess at where
                state["miss_point"] = here
            self._log("auto", "audio", here - 45, here + 50, None, None, None,
                      AUTO_RETRY_SECONDS, failures=state["failures"])
            if self.auto_follow and state["failures"] >= 2 and not self._stale(gen):
                player.pause()
                state.update(mode="probe",
                             pause_point=state.get("miss_point", here),
                             paused_at=self._now(),
                             film=(self.video_path, player))
                self.q.put(("swap", True, "auto mode: no film audio on the stream"))
                self._say("Auto: film audio not found on the stream - "
                          "assuming pause. Watching for resume...")
                return 8
            return 10
        if state.pop("held", False) and (
                state["film"] != (self.video_path, player)
                or player.is_playing()):
            # auto mode stood aside with this film paused, and meanwhile it
            # was played (a manual sync that applied, the user) or another
            # film loaded: the pause is not auto mode's to lift any more
            state.update(mode="normal", failures=0)
            return 1
        # probe: paused, waiting for the stream to resume. If the "pause"
        # was really a stretch the matcher could not hear, the stream kept
        # playing - so the window grows with the time since, or the film
        # would wait forever for audio that has already gone by. It grows
        # a step at a time, because the film's decode is cached by window
        # (in chunks of up to 900 s) and an edge that moved every probe had
        # its chunk decoded afresh every probe; and only so far, because a
        # real pause - hours of one, if the streamer is away - resumes
        # where it stopped, and a search that kept growing scanned ever
        # more of the film at every look.
        since = self._now() - state["paused_at"]
        grow = min(math.ceil(since / PAUSE_GROW_STEP) * PAUSE_GROW_STEP,
                   PAUSE_GROW_MAX)
        lo = state["pause_point"] - PAUSE_LOOK_BACK
        hi = state["pause_point"] + PAUSE_LOOK_AHEAD + grow
        hit = self._auto_probe(lo, hi)
        if hit and not self._stale(gen):
            t, score, z, t0 = hit
            off = self.placement_offset()
            self._log("resume", "audio", lo, hi, t, score, z, AUTO_RECORD_SECONDS,
                      offset=off, applied=True)
            player.sync_seek(t, t0, off)
            self.q.put(("swap", False, "auto mode: the stream resumed"))
            self._say("Auto: stream resumed - resynced.")
            self.q.put(("synced", {"match": t, "at": t0, "trusted": True,
                                   "applied": True, "source": "resume"}))
            state.update(mode="normal", failures=0)
            return self.auto_interval
        return 8

    def _stale(self, gen):
        """A manual sync started (or is running) since `gen` was read - its
        result wins over whatever auto mode found. So does a watch party
        that took the playhead while auto mode was listening: the loop
        only checks for one between passes, and a pass listens for up to
        8 s, matching after each look, before it acts."""
        return self._busy or self._gen != gen or self.session_running()

    def _auto_failed(self, state, e):
        """A pass that raised `e`; returns seconds until the next. The same
        failure every time is not a blip - the film's drive unplugged, the
        file moved - and reporting it every interval forever helps no one.
        The first of a run is reported, with one traceback in the log;
        repeats get a log line; the AUTO_FAIL_GIVEUP-th in a row says what
        is wrong once and switches auto mode off."""
        sig = (type(e).__name__, str(e))
        n = state["fail_n"] + 1 if sig == state["fail_sig"] else 1
        state.update(fail_sig=sig, fail_n=n)
        if n == 1:
            self._say(f"Auto-resync check failed: {e}")
            # the three-argument form: Python 3.9 has no one-argument one
            diagnostics.log_block("auto re-sync check failed:", "".join(
                traceback.format_exception(type(e), e, e.__traceback__)))
        elif n < AUTO_FAIL_GIVEUP:
            diagnostics.log(f"auto re-sync check failed again "
                            f"({n}/{AUTO_FAIL_GIVEUP}): {sig[0]}: {e}")
        else:
            diagnostics.log(f"auto re-sync gave up after {n} identical "
                            f"failures: {sig[0]}: {e}")
            if isinstance(e, (OSError, matcher.MatchError)):
                why = ("can't read the film's audio - check the file is "
                       "still available")
            else:
                why = f"the same error kept coming back ({e})"
            self.auto_enabled = False
            self.q.put(("auto_off", f"Auto re-sync stopped: {why}. "
                                    "Turn Keep in sync automatically back on to retry."))
        return max(self.auto_interval, 20)

    def _auto_loop(self):
        state = {"mode": "normal", "failures": 0, "pause_point": None,
                 "paused_at": None, "fail_sig": None, "fail_n": 0}
        next_at = 0.0
        while not self._closing:
            time.sleep(0.5)
            if (not self.auto_enabled or self._busy or not self.video_path
                    or self.session_running()):  # sessions own the playhead
                if state["mode"] == "probe":
                    # a pause auto mode made is still its to lift: nobody
                    # else will, and a weak Resync (not applied) leaves the
                    # film paused where normal mode would ignore it for good
                    state["held"] = True
                else:
                    state.update(mode="normal", failures=0)
                # standing aside ends a failure streak: a user retrying
                # (or re-ticking Auto) gets the full tries again
                state.update(fail_sig=None, fail_n=0)
                continue
            if time.monotonic() < next_at:
                continue
            # a pass is timed from its start: "every 30 s" is every 30 s,
            # and a held pause listens again as soon as a look ends. A
            # failure backs off from when it failed
            began = time.monotonic()
            try:
                next_at = began + self.auto_step(state)
            except Exception as e:
                next_at = time.monotonic() + self._auto_failed(state, e)
            else:
                state.update(fail_sig=None, fail_n=0)   # a clean check

    # ------------------------------------------------------ picture timing

    @property
    def measuring(self):
        return self._measuring

    def measure_stream_picture(self, stream_rect, key):
        """Time the stream's picture against its sound, on a thread: about
        MEASURE_SECONDS of its window, grabbed, against the stream's sound
        heard over the same time. stream_rect() gives the window's inside
        on screen (None once it is gone or hidden); key names the stream.
        Posts ("picture", {"what": "stream", "ok", "key", ...}) when done.
        Returns False if a measurement is already running or this
        platform cannot grab the screen."""
        if self._measuring or self.grab is None or not self.video_path:
            return False
        self._measuring = True
        threading.Thread(target=self._measure_stream_worker, args=(stream_rect, key),
                         daemon=True).start()
        return True

    def _grab_stream_cuts(self, stream_rect):
        """When the stream's video showed each cut (perf_counter times),
        and how many grabs a second saw them.

        First the whole window, to find the video in it: MEASURE_FIND
        seconds, and on a step at a time while nothing in it has moved
        enough. Those grabs time nothing - a whole window is too slow to
        grab at every refresh (picture_timing.band). Then a band through
        the video, a step at a time, until MEASURE_SECONDS have passed all
        told and MEASURE_CUTS clear cuts have been seen, or MEASURE_MAX.
        Only each band grab's change from the one before is kept, so a
        minute of them costs little."""
        pt = picture_timing
        rect = stream_rect()
        if rect is None:
            raise RuntimeError("the stream's window is not on screen")
        whole, found, spent = [], None, 0.0
        while found is None:
            if spent >= MEASURE_MAX:
                raise RuntimeError("no moving picture in the stream's window")
            step = MEASURE_STEP if whole else MEASURE_FIND
            whole.append(self.grab(rect, step, GRAB_WIDTH)[1])
            spent += step
            found = pt.video_rect(np.concatenate(whole))
        x0, y0, x1, y1 = found
        sx, sy = rect[2] / whole[0].shape[2], rect[3] / whole[0].shape[1]
        video = (rect[0] + int(x0 * sx), rect[1] + int(y0 * sy),
                 int((x1 - x0) * sx), int((y1 - y0) * sy))
        whole = None
        area, _ = pt.band(video)
        changes, last = [], None        # (times, change) per step; the last grab seen
        while True:
            times, images = self.grab(area, MEASURE_STEP, GRAB_WIDTH)
            spent += MEASURE_STEP
            norm = pt.normalize(images)
            if last is not None:
                times = np.concatenate([[last[0]], times])
                norm = np.concatenate([last[1][None], norm])
            changes.append((times[1:], pt.change(norm)))
            last = (times[-1], norm[-1])
            if spent < MEASURE_SECONDS:
                continue
            t = np.concatenate([c[0] for c in changes])
            sig = np.concatenate([c[1] for c in changes])
            if (len(pt.cuts(t, sig, threshold=pt.CLEAR_CUT)) >= MEASURE_CUTS
                    or spent >= MEASURE_MAX):
                return pt.cuts(t, sig), pt.grab_rate(t)

    def _measure_stream_worker(self, stream_rect, key):
        info = {"what": "stream", "ok": False, "key": key}
        try:
            mon = self._loopback()
            start = mon.mark()
            began = time.perf_counter()
            seen, rate = self._grab_stream_cuts(stream_rect)
            heard = time.perf_counter() - began
            samples, sr, t0, _ = mon.capture_span(heard, start=start, timeout=5.0)
            here = self.player.time()
            lo, hi = (None, None)
            if here is not None:
                here -= self.placement_offset()
                lo, hi = here - SEARCH_WINDOW, here + SEARCH_WINDOW
            match_t, score, z = audio_matcher.find_match_audio(
                self.video_path, audio_matcher.prep_capture(samples, sr), lo, hi)
            if not trusted(score, z):
                raise RuntimeError("the film's sound was not clear in the stream")
            match_t, _ = self._refine(samples, sr, match_t, "picture timing")
            lag_lo, lag_hi = picture_timing.LAG_RANGE
            pts, frames = picture_timing.decode_frames(
                self.video_path, match_t - lag_hi - 0.1, heard + lag_hi - lag_lo + 0.2, (64, 36))
            film_cuts = picture_timing.cuts(
                pts[1:], picture_timing.change(picture_timing.normalize(frames)))
            res = picture_timing.stream_lag(seen, lambda t: match_t + (t - t0), film_cuts)
            if res is None:
                raise RuntimeError(f"too few scene changes to go by ({len(seen)} seen)")
            before = self.placement_offset()
            self.picture_lags[key] = {"lag": round(res["lag"], 4), "cuts": res["cuts"],
                                      "when": time.strftime("%Y-%m-%d %H:%M")}
            self.stream_key = key
            self._shift(self.placement_offset() - before)
            info.update(ok=True, lag=res["lag"], cuts=res["cuts"], seen=res["seen"])
            diagnostics.log(f"picture: {key or 'the stream'}'s picture runs "
                            f"{res['lag'] * 1000:+.0f} ms behind its sound ({res['cuts']} of "
                            f"{res['seen']} cuts agree, spread {res['spread'] * 1000:.0f} ms; "
                            f"grabbed {rate:.0f} times a second; "
                            f"sound matched at {match_t:.3f}, z {z:.1f})")
        except Exception as e:
            info["why"] = str(e)
            diagnostics.log(f"picture: timing {key or 'the stream'} failed: {e}")
        finally:
            self._measuring = False
            self.q.put(("picture", info))

    def _refine(self, samples, sr, match_t, what):
        """(match_t made exact from the recording, True) - audio_matcher.
        refine_match - or (match_t, False) when the recording is not clear
        enough for that. A refinement is a line in the log (its size says
        how far out the matcher was), and so is a refinement that failed
        outright."""
        try:
            fine = audio_matcher.refine_match(samples, sr, self.video_path, match_t)
        except Exception as e:
            diagnostics.log(f"{what}: refining match {match_t:.3f} failed: {e}")
            return match_t, False
        if fine is None:
            return match_t, False
        diagnostics.log(f"{what}: match {match_t:.3f} refined to {fine:.4f} "
                        f"({(fine - match_t) * 1000:+.1f} ms)")
        return fine, True

    def _after_sync(self, gen, refine=False):
        """What follows a sync, on a thread: with refine, the sync made
        exact on fresh sound (its own recording could not do it); then the
        film window's own picture timed against the player's clock - only
        the built-in player's window is StreamSync's to grab. One after the
        other, as each moves the film and must not measure while the other
        does."""
        film = (self.grab is not None and self.film_picture_rect is not None
                and self.player is self.embedded and not self._closing)
        if not (refine or film):
            return

        def run():
            if refine:
                self._refine_worker(gen)
            if film:
                self._time_film_worker(gen)
        threading.Thread(target=run, daemon=True).start()

    def _refine_worker(self, gen):
        """A sync whose own recording was not clear enough to make exact (a
        quiet scene, a voice over it, a stream still settling after a jump)
        is only as good as the matcher's 16 ms steps - one left the film 21
        ms off the stream. So the stream is heard again, REFINE_LISTEN s at
        a time, up to REFINE_TRIES times, that sound made exact against
        where the film has put it, and the film moved by the difference.
        A difference over REFINE_FIX is the stream itself having moved
        since the match - a stand-in VLC's sound settled 68 ms from where a
        sync just after its seek heard it - and is followed only once a
        second look agrees with it, so a stream still on the move is not
        chased."""
        player = self.player
        moved_on = lambda: (self._gen != gen or self._closing or self.player is not player
                            or self.session_running())
        try:
            mon = self._loopback()
            seen = None                 # a big difference, waiting on a second look
            for _ in range(REFINE_TRIES):
                give_up = time.monotonic() + LEAD_SETTLE
                while player.settling() and time.monotonic() < give_up:
                    if moved_on():
                        return
                    time.sleep(0.1)
                if moved_on():
                    return
                start = mon.mark()
                samples, sr, t1, _ = mon.capture_span(REFINE_LISTEN, start=start,
                                                      timeout=REFINE_LISTEN + 5.0)
                pos = playing_position_at(player, t1)
                if moved_on():
                    return
                if pos is None or player.settling():
                    continue                # paused, or being corrected meanwhile
                off = self.placement_offset()
                fine = audio_matcher.refine_match(samples, sr, self.video_path, pos - off)
                if fine is None:
                    continue
                drift = fine + off - pos
                if abs(drift) > REFINE_FIX and (seen is None
                                                or abs(drift - seen) > REFINE_SAME):
                    seen = drift            # the stream moved: is it still moving?
                    continue
                diagnostics.log(f"sync: made exact on {REFINE_LISTEN:.0f} s more of the "
                                f"stream - the film moved {drift * 1000:+.1f} ms"
                                + (" (the stream had moved since the match; seen twice)"
                                   if abs(drift) > REFINE_FIX else ""))
                self._shift(drift)
                return
            if seen is not None:
                diagnostics.log(f"sync: the stream's sound moved {seen * 1000:+.0f} ms from "
                                "the match and no second look agreed; the film stays as "
                                "matched")
            else:
                diagnostics.log(f"sync: not made exact - the stream's sound was not clear "
                                f"enough in {REFINE_TRIES} more looks; the film stays as "
                                "matched")
        except Exception as e:
            diagnostics.log(f"sync: making the match exact stopped: {e}")

    def _time_film_worker(self, gen):
        player = self.embedded
        moved_on = lambda: self._gen != gen or self._closing
        try:
            time.sleep(1.0)
            for attempt in range(LEAD_TRIES):
                if attempt:
                    end = time.monotonic() + LEAD_RETRY
                    while time.monotonic() < end:
                        if moved_on():
                            return
                        time.sleep(0.2)
                res = self._time_film_once(player, moved_on)
                if res is None:
                    return              # moved on, or not to be timed now (logged)
                if res:
                    break
            else:
                diagnostics.log(f"picture: the film window could not be timed in {LEAD_TRIES} "
                                "tries (still or dark scenes)"
                                + ("" if self.film_lead is None else
                                   f"; still allowing {self.film_lead * 1000:.1f} ms"))
                return
            before = self.placement_offset()
            self.film_lead = (res["lead"] if self.film_lead is None
                              else 0.5 * self.film_lead + 0.5 * res["lead"])
            self._shift(self.placement_offset() - before)
            diagnostics.log(f"picture: the film window shows frames {res['lead'] * 1000:.1f} ms "
                            f"after the player's clock ({res['frames']} frames, spread "
                            f"{res['spread'] * 1000:.1f} ms, grabbed {res['rate']:.0f} times a "
                            f"second); allowing {self.film_lead * 1000:.1f} ms")
            self.q.put(("picture", {"what": "film", "ok": True, "lead": self.film_lead}))
        except Exception as e:
            diagnostics.log(f"picture: timing the film window failed: {e}")

    def _time_film_once(self, player, moved_on):
        """One look at the film window: picture_timing.film_lead's result;
        {} when the frames were not clear enough this time (try again); None
        when it is not to be timed now - moved on, or why logged."""
        give_up = time.monotonic() + LEAD_SETTLE
        while player.settling() and time.monotonic() < give_up:
            if moved_on():
                return None
            time.sleep(0.1)
        rect = self.film_picture_rect()
        skip = ("" if moved_on() else
                "the sync was still settling" if player.settling() else
                "the film is not playing" if not player.is_playing() else
                "the film window is not showing" if rect is None else None)
        if skip is not None:
            if skip:
                diagnostics.log(f"picture: the film window was not timed: {skip}")
            return None
        band, crop = picture_timing.band(rect)      # as the stream's is grabbed
        times, images = self.grab(band, LEAD_SECONDS, LEAD_WIDTH)
        pos = [playing_position_at(player, t) for t in times]
        if moved_on():
            return None
        if player.settling() or any(p is None for p in pos):
            return {}                   # corrected meanwhile: look again
        lo = min(pos) - 0.5
        pts, frames = picture_timing.decode_frames(
            self.video_path, lo, max(pos) - lo + 0.5, (images.shape[2], images.shape[1]),
            crop=crop)
        res = picture_timing.film_lead(times, picture_timing.normalize(images), pos,
                                       pts, picture_timing.normalize(frames))
        if res is None or not LEAD_RANGE[0] <= res["lead"] <= LEAD_RANGE[1]:
            return {}
        res["rate"] = picture_timing.grab_rate(times)
        return res

    # ----------------------------------------------------------- sessions

    def host(self, relay_url, password, from_player, mic_name, delay_hint, title,
             remember_relay=True):
        """remember_relay=False for this computer's own relay: its address
        lasts one party, and must not replace the relay saved for joining.
        It also means the tunnel and relay in ctl.hosting are this party's
        own (begin_hosting made them), so only the rest of the last party
        is ended."""
        self._end_last_party(keep=None if remember_relay else self.hosting)
        if remember_relay:
            self.relay_url = relay_url
        src = None
        if from_player:
            player = self.player
            src = player.clock_state
        self.session = session.HostSession(
            relay_url, self.video_path, self.q, password=password,
            position_source=src, mic_name=mic_name,
            speaker_name=self.audio_device, default_delay=delay_hint,
            title=title)
        self.session.start()

    def join(self, relay_url, code, password, mute):
        self._end_last_party()
        self.relay_url = relay_url
        self.session = session.ViewerSession(
            relay_url, code, self.video_path, self.player, self.q,
            password=password, speaker_name=self.audio_device)
        self.session.start()
        self.player.set_mute(mute)

    def leave(self):
        if self.session is not None:
            self._end_session()
        elif self.hosting is not None:
            self.stop_hosting()       # left while the tunnel was still opening

    def _end_last_party(self, keep=None):
        """What is left of the last party, ended before another starts. A
        session that ends by itself (a lost relay, a refused join) stays
        until Leave - the Watch party window says why it ended from it -
        and a tunnel host's tunnel and relay stay with it. Started over
        them, the new party's Start hosting never turned into Cancel (the
        window reads an opening tunnel as one with no session), and
        begin_hosting() dropped the old tunnel without stopping it: its
        cloudflared outlived StreamSync. `keep`: a Hosting the new party
        opened itself, left alone."""
        if self.session is not None:
            own = self.hosting if self.hosting is keep else None
            if own is not None:
                self.hosting = None          # out of _end_session's reach
            self._end_session()              # the session, and its tunnel with it
            if own is not None:
                self.hosting = own
        if self.hosting is not None and self.hosting is not keep:
            self.stop_hosting()              # a tunnel still opening: a second press

    # Hosting through this computer: hosting.Hosting runs the relay inside
    # StreamSync and a quick tunnel to it. The host's session then talks to
    # the relay directly, and viewers come in through the tunnel.

    def find_cloudflared(self):
        """(path, version, own) - see hosting.find. A few file checks and a
        `cloudflared --version`: quick enough for the Tk thread."""
        return hosting.find()

    def begin_hosting(self):
        """The Hosting for a party from this computer, made on the Tk thread
        before open_tunnel runs: a Cancel pressed at once finds it to stop,
        and the window reads 'opening' from the start. What is left of
        the last party is ended first (_end_last_party)."""
        self._end_last_party()
        h = hosting.Hosting()
        h.on_down = lambda: self._tunnel_down(h)
        self.hosting = h
        return h

    def open_tunnel(self, h, exe, download, say):
        """thread: begin_hosting()'s `h` opened - the relay here and a quick
        tunnel to it, once cloudflared is downloaded if `download`. Returns
        (local_url, public_url) once the tunnel's address resolves. Raises
        hosting.Cancelled if stop_hosting() came first, otherwise
        RuntimeError (a DownloadError for the download) with the reason.
        say(text) reports progress."""
        try:
            if download:
                say(f"Downloading cloudflared from Cloudflare's releases on GitHub "
                    f"(about {hosting.APPROX_MB} MB)...")
                shown = [0]

                def progress(done, total):
                    if h.cancelled:
                        raise hosting.Cancelled()
                    pct = int(done * 100 / total) if total else 0
                    if pct >= shown[0] + 10:
                        shown[0] = pct
                        say(f"Downloading cloudflared... {pct}%")
                exe, version = hosting.download(progress=progress)
                diagnostics.log(f"hosting: downloaded cloudflared {version} to {exe}, "
                                "checksum and Cloudflare's signature checked")
            public = h.start(exe, say)
        except BaseException as e:
            h.stop()
            if self.hosting is h:
                self.hosting = None
            if not isinstance(e, hosting.Cancelled):
                detail = getattr(e, "detail", "")
                diagnostics.log(f"hosting: could not open the tunnel: {e}"
                                + (f" ({detail})" if detail else ""))
            raise
        live = (f"in DNS after {h.dns_seconds:.1f} s" if h.dns_live
                else f"not in DNS yet after {h.dns_seconds or 0:.0f} s")
        diagnostics.log(f"hosting: relay at {h.local_url}, tunnel at {public} ({live})")
        return h.local_url, public

    def stop_hosting(self):
        """Close this computer's tunnel and relay, or cancel them opening.
        In the background: cloudflared can take seconds to exit."""
        h, self.hosting = self.hosting, None
        self.public_relay = None
        if h is not None:
            self._ended_hosting.append(h)
            threading.Thread(target=h.stop, daemon=True).start()

    def _tunnel_down(self, h):
        """cloudflared ended by itself in the middle of a party (its thread)."""
        diagnostics.log("hosting: cloudflared stopped by itself")
        if self.hosting is h and self.session_running():
            self.q.put(("session", "The Cloudflare tunnel stopped, so viewers can't reach "
                                   "the party now. Leave, then host again for a new address."))

    def _end_session(self):
        """Stop the session in the background. Stopping says goodbye to
        the relay and waits out the websocket's closing handshake - seconds
        on an unreachable relay, which is just when people reach for Leave
        - and the shells call this on the UI thread. The stop flag goes up
        before this returns, so the session's loops (a viewer's drives the
        player) stand down at their next check of it. The thread is kept
        for close(): quitting straight after Leave must not stop the
        player under a goodbye still going out."""
        sess, self.session = self.session, None
        sess.stop_flag.set()
        # this computer's relay and tunnel go after the session: its goodbye
        # goes out through them
        h, self.hosting = self.hosting, None
        self.public_relay = None
        if h is not None:
            self._ended_hosting.append(h)

        def stop():
            try:
                sess.stop()
            except Exception as e:
                diagnostics.log(f"session stop failed: {e!r}")
            if h is not None:
                try:
                    h.stop()
                except Exception as e:
                    diagnostics.log(f"hosting stop failed: {e!r}")
        closer = threading.Thread(target=stop, daemon=True)
        closer.start()
        self._closers = [t for t in self._closers if t.is_alive()] + [closer]

    # ------------------------------------------------------------ logging

    def _log(self, what, method, a, b, t, score, z, seconds, **extra):
        """One line per match decision, so a real session leaves the
        evidence needed to tune thresholds instead of nothing at all."""
        def f(v, spec):
            return "-" if v is None else format(v, spec)
        parts = [f"{what} {method}", f"window {f(a, '.1f')}..{f(b, '.1f')}",
                 f"match {f(t, '.3f')}", f"score {f(score, '.3f')}",
                 f"z {f(z, '.1f')}", f"rec {f(seconds, '.0f')}s"]
        for k, v in extra.items():
            parts.append(f"{k} {v:+.3f}" if isinstance(v, float) else f"{k} {v}")
        diagnostics.log("  ".join(parts))

    # ------------------------------------------------------------ settings

    def save_config(self, ui):
        """Write the controller's settings plus the shell's `ui` dict."""
        cfg = dict(ui)
        cfg.update({
            "video_path": self.video_path,
            "audio_device": self.audio_device,
            "auto_interval": self.auto_interval,
            "auto_follow": self.auto_follow,
            "relay_url": self.relay_url,
            "host_via": self.host_via,
            "position_from": self.position_from,
            "line_up": self.line_up,
            "picture_lags": self.picture_lags,
            "stream_key": self.stream_key,
            "film_lead": self.film_lead,
        })
        try:
            CONFIG_PATH.write_text(json.dumps(cfg))
        except OSError:
            # The log is the only witness: a save that fails silently on
            # exit looks identical to one that worked.
            diagnostics.log_block("config save FAILED:",
                                  traceback.format_exc())
            return
        diagnostics.log(f"config saved (auto={self.auto_enabled} "
                        f"follow={self.auto_follow})")

    def load_config(self):
        """Apply the controller's saved settings; return the whole dict so
        the shell can restore its own. Loads the saved film if it still
        exists (the shell must be ready for that - macOS shows its video
        window first)."""
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(cfg, dict):
            return {}
        self.audio_device = cfg.get("audio_device", "") or ""
        try:
            self.auto_interval = max(10, int(cfg.get("auto_interval", 30)))
        except (TypeError, ValueError):
            self.auto_interval = 30
        self.auto_follow = bool(cfg.get("auto_follow", True))
        if cfg.get("relay_url"):
            self.relay_url = cfg["relay_url"]
        if cfg.get("host_via") in ("tunnel", "relay"):
            self.host_via = cfg["host_via"]
        if cfg.get("position_from") in ("listen", "player"):
            self.position_from = cfg["position_from"]
        if cfg.get("line_up") in ("sound", "picture"):
            self.line_up = cfg["line_up"]
        lags = cfg.get("picture_lags")
        if isinstance(lags, dict):
            lo, hi = picture_timing.LAG_RANGE
            self.picture_lags = {
                str(k): v for k, v in lags.items()
                if isinstance(v, dict) and isinstance(v.get("lag"), (int, float))
                and lo <= v["lag"] <= hi}
        self.stream_key = str(cfg.get("stream_key") or "")
        lead = cfg.get("film_lead")
        if isinstance(lead, (int, float)) and LEAD_RANGE[0] <= lead <= LEAD_RANGE[1]:
            self.film_lead = float(lead)
        return cfg

    def film_to_restore(self, cfg):
        path = cfg.get("video_path")
        return path if path and Path(path).is_file() else None

    # ------------------------------------------------------------ shutdown

    def close(self):
        self._closing = True
        if self.session is not None:
            self._end_session()
        elif self.hosting is not None:
            self.stop_hosting()
        # give each goodbye - this session's, or one still going out from
        # a Leave just before - a moment to go out before the player
        # stops, but a dead relay must not hold the window open for its
        # whole close timeout: SESSION_CLOSE_WAIT in all, not each
        deadline = time.monotonic() + SESSION_CLOSE_WAIT
        for closer in self._closers:
            closer.join(max(0.0, deadline - time.monotonic()))
        # cloudflared is a process of its own: one still exiting now would
        # outlive StreamSync, holding a dead tunnel open
        for h in self._ended_hosting:
            h.kill()
        if self._monitor is not None:
            self._monitor.stop()
        try:
            self.embedded.stop()
        except Exception:
            pass
