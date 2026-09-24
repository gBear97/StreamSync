"""Playback backends.

EmbeddedPlayer -- libvlc rendering into our own window.
ExternalPlayer -- drives the real VLC app through its HTTP interface, which
only seeks in whole seconds: sync_seek fires the seek at the instant the
moving target crosses one.

Both are closed-loop. Measured on libvlc 3 (1080p, frame-accurate ground
truth read off the displayed frames):

- get_time() is a sampled clock: it changes every 250-500 ms and is
  exact only at the instant it changes, so a plain read lags playback by
  ~200 ms typically and up to ~600 ms. PlaybackClock dates each change as
  it arrives and is within ~1 ms.
- A seek stalls playback by an amount no constant can stand for: 75 ms
  on a 1 s GOP, 250 ms on a 10 s GOP, three times that on a busy CPU. So
  every seek is checked once playback has restarted, and what is left is
  removed by a rate trim (1.05x / 0.95x - accurate to ~10 ms, no stall,
  and inaudible since the local copy is muted) rather than another seek.
  The stall guess is learned from those checks.
- Seek-based nudges went the wrong way: a +0.1 s nudge by set_time
  netted about -170 ms, the stale get_time() and the stall together
  outweighing it. Nudges are rate trims now.
"""

import base64
import ctypes
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import vlc
    _IMPORT_ERROR = None
except Exception as e:  # missing libvlc.dll, bitness mismatch, etc.
    vlc = None
    _IMPORT_ERROR = e

EMBED_STALL_GUESS = 0.10    # first guess at a seek's stall; learned after
EXT_CMD_LEAD = 0.12         # fire external seeks this early (http + exec time)
EXT_STALL_GUESS = 0.10
TRIM_RATE = 0.05            # trims run at 1 +- this
TRIM_MAX = 0.6              # errors up to this are trimmed; beyond, re-seeked
SETTLE_TOL = 0.02           # close enough - leave it
RATE_SETTLE = 1.3           # libvlc applies a rate change ~1 s late
CLOCK_EVENTS = 4
EXT_PORT = 9723
EXT_PASSWORD = "streamsync"

if sys.platform == "darwin":
    VLC_EXE_CANDIDATES = [
        "/Applications/VLC.app/Contents/MacOS/VLC",
        os.path.expanduser("~/Applications/VLC.app/Contents/MacOS/VLC"),
    ]
else:
    VLC_EXE_CANDIDATES = [
        r"C:\Program Files\VideoLAN\VLC\vlc.exe",
        r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
    ]


class VLCError(RuntimeError):
    pass


class PlaybackClock:
    """Where playback is right now, from a clock that only updates in steps.

    Each observation is a position at the moment it first appeared. Every
    observation can only be late (delivered after the change happened),
    so the tightest of the last few - the one implying the latest
    position - is the estimate, which filters out the occasional event
    delivered 15-200 ms late. Cleared on every seek, pause and rate
    change; a seek's own immediate echo (libvlc reports the target
    before playback has moved) is ignored.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._obs = []
        self._ignore = None
        self.rate = 1.0

    def reset(self, ignore=None, rate=None, skip=0):
        """Forget what was observed. `ignore` drops the one report equal to
        a seek's target; `skip` drops the next n reports, for sources that
        echo a target without saying exactly what it was."""
        with self._lock:
            self._obs = []
            self._ignore = ignore
            self._skip = skip
            if rate is not None:
                self.rate = rate

    def observe(self, pos, at):
        with self._lock:
            if self._ignore is not None and abs(pos - self._ignore) < 0.002:
                self._ignore = None
                return
            if getattr(self, "_skip", 0) > 0:
                self._skip -= 1
                return
            if self._obs and pos <= self._obs[-1][1] + 0.001:
                # not advancing: playback is stalled or restarting, and
                # whatever was seen before it says nothing about now
                self._obs = []
            self._obs.append((at, pos))
            del self._obs[:-CLOCK_EVENTS]

    def count(self):
        with self._lock:
            return len(self._obs)

    def position(self, at=None):
        at = time.perf_counter() if at is None else at
        with self._lock:
            if not self._obs:
                return None
            off = max(p - self.rate * t for t, p in self._obs)
            return off + self.rate * at


class _ClosedLoop:
    """Put playback on a line - "at T at time c, then 1x" - and see that it
    gets there: seek (or trim, if already close), measure once playback
    has settled, trim what is left. One correction runs at a time; a new
    goal or a pause cancels it. Subclasses provide _clk, _seek_line(guess)
    and _set_rate(r)."""

    def _init_loop(self, stall_guess):
        self._clk = PlaybackClock()
        self._stall = stall_guess
        self._gen = 0
        self._line = None           # (T, c): belongs at T + (now - c)
        self._working = False
        self._trimming = False
        self._loop_lock = threading.Lock()

    def _desired(self, at):
        T, c = self._line
        return T + (at - c)

    def _cancel(self):
        with self._loop_lock:
            self._gen += 1
            self._line = None
            self._working = False
            trimming, self._trimming = self._trimming, False
        if trimming:
            self._set_rate(1.0)

    def goto(self, target, at):
        """Playback should be at `target` at perf_counter() time `at`, and
        run at 1x from there. Returns at once; the check runs behind."""
        now = time.perf_counter()
        want = target + (now - at)
        with self._loop_lock:
            if (self._working and self._line is not None
                    and abs(self._desired(now) - want) < 0.05):
                return                    # already on its way there
            self._gen += 1
            gen = self._gen
            self._line = (want, now)
            self._working = True
            trimming, self._trimming = self._trimming, False
        if trimming:
            self._set_rate(1.0)
            cur = None                     # mid-transition: not measurable
        else:
            cur = self._clk.position(now) if self._clk.count() >= 2 else None
        err = None if cur is None else cur - want
        if err is not None and abs(err) <= TRIM_MAX:
            guess = None
        else:
            guess = self._stall
            self._seek_line(guess)
        threading.Thread(target=self._correct, args=(gen, guess, err),
                         daemon=True).start()

    def _current(self, gen):
        return self._gen == gen

    def _wait(self, gen, seconds):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            if not self._current(gen):
                return False
            time.sleep(min(0.05, max(0.0, end - time.perf_counter())))
        return self._current(gen)

    def _settle(self, gen, timeout=3.0, events=2):
        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            if not self._current(gen):
                return False
            if self._clk.count() >= events:
                return True
            time.sleep(0.02)
        return False

    def _error(self):
        now = time.perf_counter()
        pos = self._clk.position(now)
        return None if pos is None else pos - self._desired(now)

    def _correct(self, gen, seek_guess, err):
        try:
            for _ in range(4):
                if seek_guess is not None:
                    if not self._settle(gen):
                        return
                    err = self._error()
                    if err is None:
                        return
                    if abs(err) < 1.5:
                        # landed at guess - stall: learn the stall
                        measured = seek_guess - err
                        self._stall = min(2.0, max(0.0, 0.6 * self._stall
                                                   + 0.4 * measured))
                    seek_guess = None
                if not self._current(gen) or abs(err) <= SETTLE_TOL:
                    return
                if abs(err) > TRIM_MAX:
                    seek_guess = self._stall
                    self._seek_line(seek_guess)
                    continue
                # ahead (err > 0): run slow; behind: run fast
                rate = 1.0 - TRIM_RATE if err > 0 else 1.0 + TRIM_RATE
                with self._loop_lock:
                    if not self._current(gen):
                        return
                    self._trimming = True
                self._set_rate(rate)
                if not self._wait(gen, abs(err) / TRIM_RATE):
                    return                 # cancelled: _cancel reset the rate
                with self._loop_lock:
                    if not self._current(gen):
                        return
                    self._trimming = False
                self._set_rate(1.0)
                if not self._wait(gen, RATE_SETTLE):
                    return
                self._clk.reset()          # events in the transition are off
                if not self._settle(gen):
                    return
                err = self._error()
                if err is None:
                    return
        finally:
            with self._loop_lock:
                if self._gen == gen:
                    self._working = False

    def nudge(self, delta):
        """Shift playback by `delta` seconds against where it is heading."""
        now = time.perf_counter()
        with self._loop_lock:
            base = self._desired(now) if (self._working and self._line) else None
        if base is None:
            base = self._clk.position(now) if self._clk.count() else self.time()
        if base is not None:
            self.goto(base + delta, now)


def _why_no_vlc():
    """The reason this machine cannot load libvlc, in the user's terms.

    Falls back to the raw import error if the probe itself cannot run:
    a vague message still beats a message about the diagnostics."""
    try:
        import diagnostics
        summary = diagnostics.vlc_summary()
        where = diagnostics.log_report("player failed to start")
        return (f"{summary}\n\n"
                f"Full details were written to:\n{where}\n\n"
                f"Details: {_IMPORT_ERROR}")
    except Exception:
        return ("Could not load VLC (libvlc).\n\n"
                "Install VLC from videolan.org (64-bit on Windows, "
                f"matching your Python).\n\nDetails: {_IMPORT_ERROR}")


def _why_no_instance():
    """libvlc is loadable but produced no instance - say what is missing."""
    try:
        import diagnostics
        info = diagnostics.probe_vlc()
        where = diagnostics.log_report("libvlc gave no instance")
        detail = info.get("plugins") or "could not locate VLC's plugins"
        return ("VLC loaded, but would not start.\n\n"
                "This is normally its plugins folder being missing or "
                "unreadable; reinstalling VLC restores it.\n\n"
                f"Plugins: {detail}\n\nFull details: {where}")
    except Exception:
        return "Could not create a VLC instance."


def _tk_nsview(win_id):
    """The NSView behind a Tk widget on macOS, or None.

    Tk's own C library is already loaded into this process the moment
    tkinter is imported, so the symbol is resolved from the process image
    rather than from a guessed dylib path - the path differs between a
    source checkout and a frozen app, and the loaded copy is by
    definition the right one.
    """
    try:
        lib = ctypes.CDLL(None)
        f = lib.TkMacOSXGetRootControl
    except (OSError, AttributeError, TypeError):
        # TypeError: Windows' CDLL cannot open "the process image" at all
        # (it tests the name for path separators, and None has none)
        return None
    f.restype = ctypes.c_void_p
    f.argtypes = [ctypes.c_void_p]
    try:
        return f(win_id)
    except Exception:
        return None


class EmbeddedPlayer(_ClosedLoop):
    def __init__(self, hwnd=None):
        """hwnd: Tk window handle to render into (Windows). On macOS pass
        None and call attach_tk() with a Tk widget id once one exists.

        The first Mac field test shipped on the assumption that with no
        drawable libvlc "opens its own video window". It does not: the
        macOS vout needs an NSView it can render into, and without one
        playback runs - audio, position, a successful sync - while no
        window ever appears. python-vlc's own Tk example embeds via
        TkMacOSXGetRootControl for exactly this reason, and attach_tk
        below is that technique."""
        if vlc is None:
            # "Install VLC from videolan.org" was the whole message here,
            # which is wrong advice for every cause except the one where
            # VLC is genuinely absent - and it reads the same when VLC is
            # installed but built for the other processor. Ask what is
            # actually on this machine and say that instead.
            raise VLCError(_why_no_vlc())
        self.instance = vlc.Instance("--no-video-title-show", "--quiet")
        if self.instance is None:
            # libvlc loaded but would not start. Nearly always its plugin
            # directory: the library alone cannot decode anything, and it
            # reports that by handing back a null instance rather than by
            # saying so.
            raise VLCError(_why_no_instance())
        self.mp = self.instance.media_player_new()
        self._init_loop(EMBED_STALL_GUESS)
        self._watch_time()
        self.embedded = False
        if hwnd is not None:
            self.mp.set_hwnd(int(hwnd))
            # let Tk keep mouse/keyboard events, not the VLC child window
            self.mp.video_set_mouse_input(False)
            self.mp.video_set_key_input(False)
            self.embedded = True
        self.has_media = False

    def _watch_time(self):
        """Feed libvlc's own "time changed" events to the clock, dated the
        moment they arrive - far tighter than polling get_time()."""
        def changed(event):
            ms = event.u.new_time
            if ms > 0:
                self._clk.observe(ms / 1000.0, time.perf_counter())
        self._on_time = changed        # keep a reference: libvlc holds none
        self.mp.event_manager().event_attach(
            vlc.EventType.MediaPlayerTimeChanged, changed)

    @classmethod
    def _around(cls, instance, mp):
        """A player around an existing libvlc instance and media player -
        how tests (and the timing harness) supply their own."""
        self = cls.__new__(cls)
        self.instance, self.mp = instance, mp
        self._init_loop(EMBED_STALL_GUESS)
        self._watch_time()
        self.embedded = False
        self.has_media = False
        return self

    def _seek_line(self, guess):
        t = max(0.0, self._desired(time.perf_counter()) + guess)
        ms = int(round(t * 1000))
        self._clk.reset(ignore=ms / 1000.0)
        self.mp.set_time(ms)

    def _set_rate(self, rate):
        self._clk.reset(rate=rate)
        self.mp.set_rate(rate)

    def attach_tk(self, win_id):
        """Render into the Tk widget with this winfo_id() (macOS).

        Must happen before playback starts; safe to call once, cheap to
        skip. Returns whether embedding actually took, because the
        fallback - libvlc with no drawable - plays sound to an invisible
        player, and the caller should say so rather than let anyone
        watch a black nothing and doubt their film.
        """
        if self.embedded:
            return True
        # No platform gate: off macOS the symbol simply does not exist
        # and _tk_nsview says None, which is also the honest answer.
        view = _tk_nsview(int(win_id))
        if not view:
            return False
        self.mp.set_nsobject(view)
        self.embedded = True
        return True

    def load(self, path):
        self.mp.set_media(self.instance.media_new(path))
        self.has_media = True

    def ensure_playing(self, timeout=6.0):
        if not self.has_media:
            raise VLCError("No video file loaded.")
        if not self.mp.is_playing():
            self._clk.reset()
            self.mp.play()
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if self.mp.is_playing() and self.mp.get_time() >= 0:
                return
            time.sleep(0.05)
        raise VLCError("VLC did not start playback in time.")

    def sync_seek(self, match_t, t0_perf, offset):
        """Line playback up with a stream moment captured at t0_perf: the
        film belongs at match_t + offset then, and 1x from there."""
        self.ensure_playing()
        self.goto(match_t + offset, t0_perf)

    def toggle_pause(self):
        if not self.has_media:
            return
        self._cancel()
        self._clk.reset()
        state = self.mp.get_state()
        if state in (vlc.State.Ended, vlc.State.Stopped, vlc.State.NothingSpecial):
            self.mp.play()
        else:
            self.mp.pause()

    def pause(self):
        self._cancel()
        if self.mp.is_playing():
            self._clk.reset()
            self.mp.set_pause(1)

    def seek(self, seconds):
        self._cancel()
        ms = max(0, int(round(seconds * 1000)))
        self._clk.reset(ignore=ms / 1000.0)
        self.mp.set_time(ms)

    def time(self):
        if self.mp.is_playing() and self._clk.count():
            return self._clk.position()
        t = self.mp.get_time()
        return None if t is None or t < 0 else t / 1000.0

    def clock(self):
        """(position, perf_counter when it was true) while playing and
        measurable, else None - a position is only useful for timing if
        it is dated, and only trustworthy once the clock has settled."""
        if not self.mp.is_playing() or self._clk.count() < 2 or self._trimming:
            return None
        now = time.perf_counter()
        return self._clk.position(now), now

    def clock_state(self):
        """(position, playing, perf_counter) for a watch-party host."""
        now = time.perf_counter()
        playing = self.is_playing()
        pos = self._clk.position(now) if playing and self._clk.count() else None
        return (self.time() if pos is None else pos), playing, now

    def length(self):
        n = self.mp.get_length()
        return None if n is None or n <= 0 else n / 1000.0

    def set_mute(self, mute):
        self.mp.audio_set_mute(bool(mute))

    def set_fullscreen(self, flag):
        """Fullscreen for the libvlc-owned video window (macOS mode)."""
        self.mp.set_fullscreen(bool(flag))

    def is_playing(self):
        return bool(self.mp.is_playing())

    def stop(self):
        self._cancel()
        self.mp.stop()

    # --- subtitles (embedded only; external mode uses VLC's own menu) ---

    def subtitle_tracks(self):
        try:
            descs = self.mp.video_get_spu_description() or []
        except Exception:
            return []
        out = []
        for tid, name in descs:
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            out.append((int(tid), str(name)))
        return out

    def set_subtitle(self, track_id):
        self.mp.video_set_spu(int(track_id))

    def add_subtitle_file(self, path):
        uri = Path(path).absolute().as_uri()
        self.mp.add_slave(vlc.MediaSlaveType.subtitle, uri, True)


class ExternalPlayer(_ClosedLoop):
    """Controls a real VLC window via its built-in HTTP interface.

    The interface reports time and length in whole seconds, so position is
    `position` (a full-precision fraction) times the file's duration as
    ffmpeg measures it. A poller watches for that value to change - like
    get_time() it only moves every few hundred ms - and feeds the changes
    to the same PlaybackClock the embedded player uses.
    """

    POLL = 0.05

    def __init__(self, exe=None, port=EXT_PORT, password=EXT_PASSWORD):
        self.exe = exe or self._find_vlc()
        self.port = port
        self.password = password
        self.proc = None
        self.has_media = False
        self.duration = None
        self._playing = False
        self._auth = "Basic " + base64.b64encode(
            (":" + password).encode()).decode()
        self._init_loop(EXT_STALL_GUESS)
        self._poller = None
        self._stop_poll = threading.Event()

    @staticmethod
    def _find_vlc():
        for p in VLC_EXE_CANDIDATES:
            if Path(p).is_file():
                return p
        raise VLCError("Could not find vlc.exe - install VLC from videolan.org.")

    def _request(self, params=None, timeout=2.0):
        url = f"http://127.0.0.1:{self.port}/requests/status.json"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": self._auth})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _cmd(self, command, val=None):
        params = {"command": command}
        if val is not None:
            params["val"] = val
        return self._request(params)

    def _alive(self):
        try:
            self._request(timeout=0.6)
            return True
        except Exception:
            return False

    def load(self, path):
        try:
            from matcher import probe
            self.duration = probe(str(path))[0]
        except Exception:
            self.duration = None
        if self._alive():
            self._cmd("in_play", str(path))
        else:
            self.proc = subprocess.Popen([
                self.exe, "--extraintf", "http",
                "--http-host", "127.0.0.1",
                "--http-port", str(self.port),
                "--http-password", self.password,
                "--no-one-instance", "--no-video-title-show",
                str(path),
            ])
            deadline = time.perf_counter() + 12.0
            while time.perf_counter() < deadline:
                if self._alive():
                    break
                time.sleep(0.25)
            else:
                raise VLCError("External VLC did not come up with its HTTP "
                               "interface enabled.")
        self.has_media = True
        self._clk.reset()
        if self._poller is None or not self._poller.is_alive():
            self._stop_poll.clear()
            self._poller = threading.Thread(target=self._poll, daemon=True)
            self._poller.start()

    def _poll(self):
        last = None
        while not self._stop_poll.is_set():
            before = time.perf_counter()
            st = self._status(timeout=0.5)
            after = time.perf_counter()
            if st:
                self._playing = st.get("state") == "playing"
                pos = self._position(st)
                if self._playing and pos is not None and pos != last:
                    self._clk.observe(pos, (before + after) / 2)
                last = pos
            self._stop_poll.wait(self.POLL)

    def _status(self, timeout=1.0):
        try:
            return self._request(timeout=timeout)
        except Exception:
            return None

    def ensure_playing(self, timeout=8.0):
        if not self.has_media:
            raise VLCError("No video file loaded in external VLC.")
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            st = self._status()
            if st and st.get("state") == "playing":
                return
            if st and st.get("state") in ("paused", "stopped"):
                try:
                    self._clk.reset()
                    self._cmd("pl_forceresume")
                    if st.get("state") == "stopped":
                        self._cmd("pl_play")
                except Exception:
                    pass
            time.sleep(0.2)
        raise VLCError("External VLC did not start playing in time.")

    def sync_seek(self, match_t, t0_perf, offset):
        self.ensure_playing()
        self.goto(match_t + offset, t0_perf)

    def _seek_line(self, guess):
        """HTTP seeks are whole-second only: pick the next whole second the
        line will reach and fire the seek at exactly that wall moment."""
        def target():
            return self._desired(time.perf_counter()) + guess

        n = math.floor(target()) + 1
        if n - target() < 0.35:  # too tight to schedule reliably
            n += 1
        while True:
            d = (n - EXT_CMD_LEAD) - target()
            if d <= 0:
                break
            time.sleep(min(d, 0.05))
        self._clk.reset(skip=1)          # the status echoes the target first
        try:
            self._cmd("seek", str(int(n)))
        except Exception:
            pass

    def _set_rate(self, rate):
        self._clk.reset(rate=rate)
        try:
            self._cmd("rate", f"{rate:.3f}")
        except Exception:
            pass

    def toggle_pause(self):
        self._cancel()
        self._clk.reset()
        try:
            self._cmd("pl_pause")
        except Exception:
            pass

    def pause(self):
        self._cancel()
        self._clk.reset()
        try:
            self._cmd("pl_forcepause")
        except Exception:
            pass

    def seek(self, seconds):
        self._cancel()
        self._clk.reset(skip=1)
        try:
            self._cmd("seek", str(int(round(seconds))))
        except Exception:
            pass

    def time(self):
        if self._playing and self._clk.count():
            return self._clk.position()
        return self._position(self._status())

    def _position(self, st):
        if not st:
            return None
        pos = st.get("position")
        if self.duration and isinstance(pos, (int, float)) and pos > 0:
            return float(pos) * self.duration
        length = st.get("length") or 0
        if length > 0 and isinstance(pos, (int, float)) and pos > 0:
            return float(pos) * float(length)  # whole-second length: rough
        t = st.get("time")
        return float(t) if isinstance(t, (int, float)) and t >= 0 else None

    def clock(self):
        """(position, perf_counter when it was true) while playing."""
        if not self._playing or self._clk.count() < 2 or self._trimming:
            return None
        now = time.perf_counter()
        return self._clk.position(now), now

    def clock_state(self):
        now = time.perf_counter()
        if self._playing and self._clk.count():
            return self._clk.position(now), True, now
        st = self._status()
        return self._position(st), bool(st and st.get("state") == "playing"), now

    def length(self):
        if self.duration:
            return self.duration
        st = self._status()
        if st and (st.get("length") or 0) > 0:
            return float(st["length"])
        return None

    def set_mute(self, mute):
        try:
            self._cmd("volume", "0" if mute else "256")
        except Exception:
            pass

    def is_playing(self):
        st = self._status()
        return bool(st and st.get("state") == "playing")

    def fullscreen_toggle(self):
        try:
            self._cmd("fullscreen")
        except Exception:
            pass

    def stop(self):
        # leave the user's VLC window alone on app close
        self._cancel()
        self._stop_poll.set()
