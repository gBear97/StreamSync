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

import audio_capture
import audio_matcher
import diagnostics
import hosting
import matcher
import session
from players import ExternalPlayer, VLCError

CONFIG_PATH = Path.home() / ".streamsync.json"
SEARCH_WINDOW = 120.0      # a hinted sync searches this far either side
AUDIO_SYNC_SECONDS = 6.0   # manual sync recording length...
AUDIO_RETRY_SECONDS = (12.0, 18.0)  # ...extended this far when it is weak
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
        self.audio_device = ""       # substring of the capture device name
        self.auto_enabled = False
        self.auto_follow = True
        self.auto_interval = 30
        self.relay_url = "ws://localhost:8765"
        self.host_via = "tunnel"     # "tunnel": host through this computer; "relay": relay_url
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
        here = t - self.offset          # where the stream should be
        return self.search(here - SEARCH_WINDOW, here + 30.0, mute)

    def search(self, a, b, mute=True):
        """Start a sync of the stretch a..b (None for the whole film) in
        the background."""
        if not self._claim():
            return False
        args = (a, b, self.player, self.offset, mute)
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
            if apply:
                self._say("Seeking...")
                player.sync_seek(match_t, t0, offset,
                                 waiting=lambda: self._say(self._opening_hint()))
                player.set_mute(mute)
                self.q.put(("swap", False, "a sync moved the film"))
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
            here = clk[0] - self.offset       # where the stream should be
            hit = self._auto_probe(here - 45, here + 50)
            if hit and not self._stale(gen):
                t, score, z, t0 = hit
                state["failures"] = 0
                # the player is meant to sit at stream + offset
                pos_t0 = playing_position_at(player, t0)
                drift = None if pos_t0 is None else (t + self.offset) - pos_t0
                self._log("auto", "audio", here - 45, here + 50, t, score, z,
                          AUTO_RECORD_SECONDS, drift=drift, offset=self.offset,
                          applied=drift is not None and abs(drift) > DRIFT_TOLERANCE)
                if drift is not None and abs(drift) > DRIFT_TOLERANCE:
                    player.sync_seek(t, t0, self.offset)
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
            self._log("resume", "audio", lo, hi, t, score, z, AUTO_RECORD_SECONDS,
                      offset=self.offset, applied=True)
            player.sync_seek(t, t0, self.offset)
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

    # ----------------------------------------------------------- sessions

    def host(self, relay_url, password, from_player, mic_name, delay_hint, title,
             remember_relay=True):
        """remember_relay=False for this computer's own relay: its address
        lasts one party, and must not replace the relay saved for joining."""
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
        and the window reads 'opening' from the start."""
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
