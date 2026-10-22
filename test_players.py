"""The closed-loop player, against a simulated libvlc.

The simulation reproduces what was measured on real libvlc 3 (1080p,
ground truth read off the displayed frames):
- get_time() changes only every 250-500 ms, exact at the change, then
  stale; the change event is occasionally delivered 50-150 ms late;
- set_time() echoes its target at once, then playback stalls for a while
  before it restarts - an amount that varies per seek - unless paused;
- set_rate() takes effect about 1 s late, and so does going back to 1x.

On real libvlc the same code measured: sync error 5-9 ms median (was
82-179 ms), position estimate within 1 ms, +0.1 s nudge nets +96 ms (a
seek-based nudge netted about -170 ms). This test holds the logic to
that without needing VLC installed.

It also pins what loading a film sends each player - external VLC's HTTP
commands and command line, the embedded player's drawable and input
flags - with fakes that record the calls, and the embedded player's frame
rate and its exact step while paused (the fullscreen bar's +-1 frame).
And external VLC's time, length and play state come from its poller,
not from HTTP on the reading (Tk) thread, even while VLC hangs.
"""

import os
import pathlib
import random
import sys
import threading
import time
import types

import players


class FakeMP:
    """libvlc's media player as measured, on the real clock."""

    def __init__(self, stall=0.3, seed=1):
        self.rng = random.Random(seed)
        self.stall = stall
        self.lock = threading.Lock()
        # piecewise-linear truth: list of (perf, pos, rate) from that time on
        self.segments = [(time.perf_counter(), 0.0, 0.0)]
        self.playing = False
        self.callbacks = []
        self.last_report = 0
        self.rates = []            # (apply_at, rate)
        self.alive = True
        threading.Thread(target=self._ticker, daemon=True).start()

    # the truth ----------------------------------------------------------
    def truth(self, at=None):
        at = time.perf_counter() if at is None else at
        with self.lock:
            self._apply_rates(at)
            seg = max((s for s in self.segments if s[0] <= at),
                      key=lambda s: s[0], default=self.segments[0])
        t0, p0, r = seg
        return p0 + r * (at - t0)

    def _apply_rates(self, at):
        for when, r in sorted(self.rates):
            if when <= at:
                t0, p0, r0 = max(self.segments, key=lambda s: s[0])
                if when >= t0:
                    self.segments.append((when, p0 + r0 * (when - t0),
                                          r if r0 else 0.0))
                self.rates.remove((when, r))
                self.rate_now = r

    def _set_segment(self, at, pos, rate):
        with self.lock:
            self.segments = [s for s in self.segments if s[0] < at]
            self.segments.append((at, pos, rate))

    # events -------------------------------------------------------------
    def _ticker(self):
        while self.alive:
            time.sleep(self.rng.choice((0.25, 0.25, 0.25, 0.5)))
            if not self.playing or self._frozen():
                continue            # libvlc reports nothing mid-seek
            # Delivered `late` after the change it reports. Simulated by
            # reporting the value from `late` ago rather than by sleeping,
            # so the lateness is exactly this and not also however far a
            # CI runner's sleep overshoots (5-12 ms on macOS runners).
            late = self.rng.uniform(0, 0.004)
            if self.rng.random() < 0.08:
                late = self.rng.uniform(0.05, 0.15)
            value = int(self.truth(time.perf_counter() - late) * 1000)
            if value <= 0:
                continue
            self.last_report = value
            self._emit(value)

    def _frozen(self, at=None):
        at = time.perf_counter() if at is None else at
        with self.lock:
            seg = max((x for x in self.segments if x[0] <= at),
                      key=lambda x: x[0], default=self.segments[0])
        return seg[2] == 0.0

    def _emit(self, ms):
        ev = types.SimpleNamespace(u=types.SimpleNamespace(new_time=ms))
        for cb in list(self.callbacks):
            cb(ev)

    def event_manager(self):
        mp = self

        class EM:
            def event_attach(self, _type, cb):
                mp.callbacks.append(cb)
        return EM()

    # the libvlc surface the player uses ----------------------------------
    def play(self):
        now = time.perf_counter()
        self._set_segment(now, self.truth(now), self._rate())
        self.playing = True

    def _rate(self):
        return getattr(self, "rate_now", 1.0)

    def is_playing(self):
        return self.playing

    def get_time(self):
        return self.last_report if self.playing else int(self.truth() * 1000)

    def set_time(self, ms):
        now = time.perf_counter()
        stall = self.stall * self.rng.uniform(0.8, 1.2)
        self._emit(ms)                               # the echo
        self.last_report = ms
        with self.lock:
            self.segments = [s for s in self.segments if s[0] < now]
            self.segments.append((now, ms / 1000.0, 0.0))          # frozen...
            if self.playing:                 # ...and a paused film stays so
                self.segments.append((now + stall, ms / 1000.0, self._rate()))

    def set_rate(self, r):
        with self.lock:
            self.rates.append((time.perf_counter() + 1.0, r))       # ~1 s late

    def set_pause(self, flag):
        now = time.perf_counter()
        self._set_segment(now, self.truth(now), 0.0 if flag else self._rate())
        self.playing = not flag

    def pause(self):
        self.set_pause(self.playing)            # libvlc's pause() toggles

    def get_state(self):
        return "playing" if self.playing else "paused"

    def stop(self):
        self.alive = False

    def set_media(self, m):
        pass

    def audio_set_mute(self, m):
        pass


FAKE_VLC = types.SimpleNamespace(
    EventType=types.SimpleNamespace(MediaPlayerTimeChanged=1),
    State=types.SimpleNamespace(Ended="ended", Stopped="stopped",
                                NothingSpecial="none", Error="error",
                                Opening="opening", Buffering="buffering",
                                Playing="playing", Paused="paused"))


def make(stall=0.3, seed=1):
    players.vlc = FAKE_VLC
    mp = FakeMP(stall=stall, seed=seed)
    p = players.EmbeddedPlayer._around(None, mp)
    p.has_media = True
    mp.play()
    time.sleep(1.2)
    return p, mp


def wait_done(p, timeout=30):
    end = time.monotonic() + timeout
    time.sleep(0.2)
    while p._working and time.monotonic() < end:
        time.sleep(0.05)
    assert not p._working, "correction never finished"


class FakeHTTP:
    """External VLC's HTTP interface: up or not, recording each command
    with the duration the player held at the moment it was sent."""

    def __init__(self, player, up=True):
        self.player = player
        self.up = up
        self.sent = []
        self.refuse = set()        # commands that fail on the wire
        player._request = self.request

    def request(self, params=None, timeout=2.0):
        if not self.up:
            raise OSError("connection refused")
        if params:
            self.sent.append((dict(params), self.player.duration))
            if params["command"] in self.refuse:
                raise OSError("connection reset")
        return {"state": "playing", "position": 0.5, "length": 100}

    def commands(self, name):
        return [(params, dur) for params, dur in self.sent
                if params["command"] == name]


FILM_LENGTHS = {"First Film.mkv": 5400.0, "Second Film.mkv": 7200.0,
                "Third Film.mkv": 6000.0}


def external_load():
    """External VLC loads: the second film really reaches VLC, the
    spawn suits the platform, Tk's forward-slash paths arrive native,
    and the length the clock scales by changes only once VLC has
    accepted the film."""
    def probe(path):
        return FILM_LENGTHS[pathlib.Path(path).name], 0.0

    saved = sys.modules.get("matcher"), players.subprocess, players.sys
    sys.modules["matcher"] = types.SimpleNamespace(probe=probe)  # no ffmpeg
    made = []
    try:
        # a film is playing in a running VLC; the picker hands us another
        p = players.ExternalPlayer(exe="vlc")
        made.append(p)
        http = FakeHTTP(p)
        p.duration, p.has_media = FILM_LENGTHS["First Film.mkv"], True
        picked = "C:/Films/Second Film.mkv"      # as Tk's file dialog has it
        native = str(pathlib.Path(picked))
        p.load(picked)
        plays = http.commands("in_play")
        assert len(plays) == 1, http.sent
        params, dur_then = plays[0]
        # VLC's in_play reads `input`; `val` is ignored without an error
        assert params == {"command": "in_play", "input": native}, params
        if os.name == "nt":
            assert "/" not in params["input"], params
        # still the first film's length while VLC was still playing it
        assert dur_then == FILM_LENGTHS["First Film.mkv"], dur_then
        assert p.duration == FILM_LENGTHS["Second Film.mkv"], p.duration
        print(f"external VLC, running: in_play sent {params['input']!r} as "
              f"input=, length {dur_then:.0f} s -> {p.duration:.0f} s "
              f"only after it")

        # VLC never takes the third film: the second is still playing,
        # and so is its length
        http.refuse.add("in_play")
        try:
            p.load("C:/Films/Third Film.mkv")
        except OSError:
            pass
        else:
            raise AssertionError("a failed in_play was not reported")
        assert p.duration == FILM_LENGTHS["Second Film.mkv"], p.duration
        print("external VLC, in_play fails: the playing film keeps its length")

        # no VLC running: spawn one with the film on its command line
        for platform in ("win32", "darwin", "linux"):
            p = players.ExternalPlayer(exe="vlc")
            made.append(p)
            http = FakeHTTP(p, up=False)
            spawned = []

            def popen(argv, http=http, spawned=spawned):
                spawned.append((list(argv), http.player.duration))
                http.up = True
                return types.SimpleNamespace()

            players.subprocess = types.SimpleNamespace(Popen=popen)
            players.sys = types.SimpleNamespace(platform=platform)
            p.load(picked)
            assert len(spawned) == 1, spawned
            argv, dur_then = spawned[0]
            # Cocoa VLC exits on an option it does not know
            assert ("--no-one-instance" in argv) == (platform != "darwin"), \
                (platform, argv)
            assert argv[-1] == native, argv
            assert dur_then is None, dur_then
            assert p.duration == FILM_LENGTHS["Second Film.mkv"], p.duration
            print(f"external VLC, spawned on {platform}: "
                  f"--no-one-instance {'--no-one-instance' in argv}, "
                  f"film {argv[-1]!r}")
    finally:
        real_matcher, players.subprocess, players.sys = saved
        if real_matcher is None:
            sys.modules.pop("matcher", None)
        else:
            sys.modules["matcher"] = real_matcher
        for p in made:
            p.stop()


class HangingHTTP:
    """External VLC's HTTP interface, which can hang: while `hung` is set
    a request takes its whole timeout and fails, as one to a stuck VLC
    does. Playing moves the position at 1x over a 100 s film. Records the
    thread each request came from, and counts the ones answered to
    another thread (the poller)."""

    def __init__(self, player):
        self.hung = threading.Event()
        self.state = "paused"
        self.pos, self.since = 25.0, time.perf_counter()
        self.callers = []
        self.polled = 0
        player._request = self.request

    def play(self):
        self.pos, self.since, self.state = self.where(), time.perf_counter(), "playing"

    def where(self):
        run = time.perf_counter() - self.since if self.state == "playing" else 0.0
        return self.pos + run

    def request(self, params=None, timeout=2.0):
        self.callers.append(threading.current_thread())
        if self.hung.is_set():
            time.sleep(timeout)
            raise TimeoutError("timed out")
        t = self.where()
        if threading.current_thread() is not threading.main_thread():
            self.polled += 1
        return {"state": self.state, "position": t / 100.0, "length": 100,
                "time": int(t)}

    def asked_from_here(self, since):
        return sum(c is threading.current_thread() for c in self.callers[since:])


def external_cached_status():
    """The window reads time(), length() and is_playing() on the Tk
    thread every 700 ms. Once a film is loaded they answer from what the
    poller last heard, without a request of their own, so a hung VLC
    cannot freeze the window. And a VLC that stops answering is no longer
    reported as playing on: not after EXT_STATUS_STALE."""
    def no_probe(path):
        raise OSError("no ffprobe")      # length comes from VLC's status

    saved = sys.modules.get("matcher"), players.EXT_STATUS_STALE
    sys.modules["matcher"] = types.SimpleNamespace(probe=no_probe)
    players.EXT_STATUS_STALE = 0.4
    p = players.ExternalPlayer(exe="vlc")
    http = HangingHTTP(p)
    try:
        p.load("C:/Films/First Film.mkv")
        assert p.duration is None, p.duration
        deadline = time.perf_counter() + 5
        while http.polled < 2 and time.perf_counter() < deadline:
            time.sleep(0.02)

        def reads():
            mark, start = len(http.callers), time.perf_counter()
            got = p.time(), p.length(), p.is_playing()
            took = time.perf_counter() - start
            asked = http.asked_from_here(mark)
            assert asked == 0, f"{asked} HTTP request(s) from the reading thread"
            assert took < 0.25, f"reads took {took:.2f} s"
            return got

        t, n, playing = reads()
        assert (t, n, playing) == (25.0, 100.0, False), (t, n, playing)
        http.hung.set()
        t, n, playing = reads()
        assert (t, n, playing) == (25.0, 100.0, False), (t, n, playing)
        print("external VLC, paused: time, length and play state read with "
              "no request of their own, and still while VLC hangs")

        http.hung.clear()
        http.play()
        deadline = time.perf_counter() + 5
        while p.clock() is None and time.perf_counter() < deadline:
            time.sleep(0.02)
        t, n, playing = reads()
        assert playing and n == 100.0, (t, n, playing)
        assert abs(t - http.where()) < 0.2, (t, http.where())
        http.hung.set()
        hung_at = time.perf_counter()
        t, n, playing = reads()
        assert playing and n == 100.0, (t, n, playing)
        time.sleep(1.2)                  # past the stale limit, two polls failed
        t, n, playing = reads()
        assert (t, n, playing) == (None, None, False), (t, n, playing)
        assert p.clock() is None
        print(f"external VLC, playing then hung: still playing at once, "
              f"nothing claimed {time.perf_counter() - hung_at:.1f} s later "
              f"(stale after {players.EXT_STATUS_STALE} s)")
    finally:
        http.hung.clear()
        p.stop()
        real_matcher, players.EXT_STATUS_STALE = saved
        if real_matcher is None:
            sys.modules.pop("matcher", None)
        else:
            sys.modules["matcher"] = real_matcher


class AnchorMP:
    """The libvlc media player calls a load, a fullscreen and the input
    flags make, in order."""

    def __init__(self):
        self.calls = []

    def event_manager(self):
        return types.SimpleNamespace(event_attach=lambda _type, cb: None)

    def set_media(self, m):
        self.calls.append(("set_media", m))

    def set_hwnd(self, h):
        self.calls.append(("set_hwnd", h))

    def set_nsobject(self, v):
        self.calls.append(("set_nsobject", v))

    def set_fullscreen(self, flag):
        self.calls.append(("set_fullscreen", flag))

    def video_set_mouse_input(self, flag):
        self.calls.append(("mouse_input", flag))

    def video_set_key_input(self, flag):
        self.calls.append(("key_input", flag))


class AnchorInstance:
    def __init__(self, mp):
        self.mp = mp

    def media_player_new(self):
        return self.mp

    def media_new(self, path):
        return ("media", path)


def slow_start():
    """A first play whose file open is held up - macOS asking whether
    StreamSync may open the film's folder held one for 58 s, a drive can
    spin up, a share can be slow - is waited out and reported, where a
    fixed 6 s once failed the sync and the film then started anyway from
    0:00. A film that will not play still ends the wait at once, and an
    ended film is stopped before it plays again (libvlc 3 ignores play()
    on an ended player)."""
    S = FAKE_VLC.State

    class SlowMP:
        def __init__(self, opens_after=None, fails=False, state=S.NothingSpecial):
            self.opens_after, self.fails = opens_after, fails
            self.state, self.began, self.calls = state, None, []

        def play(self):
            self.calls.append("play")
            if self.state == S.Ended:
                return                   # what libvlc 3 does: nothing
            self.state, self.began = S.Opening, time.perf_counter()

        def stop(self):
            self.calls.append("stop")
            self.state, self.began = S.Stopped, None

        def _tick(self):
            if self.state == S.Opening:
                waited = time.perf_counter() - self.began
                if self.fails and waited > 0.05:
                    self.state = S.Ended      # a gone film: Ended, not Error
                elif self.opens_after is not None and waited >= self.opens_after:
                    self.state = S.Playing

        def get_state(self):
            self._tick()
            return self.state

        def is_playing(self):
            self._tick()
            return self.state == S.Playing

        def get_time(self):
            return 0 if self.is_playing() else -1

        def event_manager(self):
            return types.SimpleNamespace(event_attach=lambda *a: None)

    def player(mp):
        p = players.EmbeddedPlayer._around(None, mp)
        p.has_media = True
        return p

    saved = players.vlc, players.SLOW_START
    players.vlc, players.SLOW_START = FAKE_VLC, 0.3
    try:
        # held up 1.2 s: waited out, reported once, past SLOW_START
        told = []
        began = time.perf_counter()
        player(SlowMP(opens_after=1.2)).ensure_playing(
            timeout=5.0, waiting=lambda: told.append(time.perf_counter() - began))
        took = time.perf_counter() - began
        assert 1.2 <= took < 1.6, took
        assert len(told) == 1 and 0.3 <= told[0] < 0.6, told

        # never opens: gives up at the timeout, saying where to look
        began = time.perf_counter()
        try:
            player(SlowMP()).ensure_playing(timeout=0.8)
            raise AssertionError("a film that never opened was waited on forever")
        except players.VLCError as e:
            assert 0.8 <= time.perf_counter() - began < 1.2
            assert "still opening" in str(e) and "folder" in str(e), e

        # will not play (gone): fails at once, not after the timeout
        began = time.perf_counter()
        try:
            player(SlowMP(fails=True)).ensure_playing(timeout=30.0)
            raise AssertionError("a film that will not play was waited on")
        except players.VLCError as e:
            assert time.perf_counter() - began < 1.5, time.perf_counter() - began
            assert "could not play" in str(e), e

        # stopped while it waits (the Stop button): said so, not "gone"
        mp = SlowMP()
        p = player(mp)
        threading.Timer(0.3, mp.stop).start()
        try:
            p.ensure_playing(timeout=5.0)
            raise AssertionError("a start stopped midway kept waiting")
        except players.VLCError as e:
            assert "stopped before" in str(e), e

        # ended: stopped first, then played - and it plays
        mp = SlowMP(opens_after=0.0, state=S.Ended)
        player(mp).ensure_playing(timeout=2.0)
        assert mp.calls == ["stop", "play"], mp.calls
        mp = SlowMP(opens_after=0.0, state=S.Ended)
        player(mp).toggle_pause()
        assert mp.calls == ["stop", "play"], mp.calls

        # a sync hands its waiting() on to the start
        p = player(SlowMP(opens_after=0.0))
        seen = []
        p.ensure_playing = lambda waiting=None: seen.append(waiting)
        p.goto = lambda *a: None
        cb = lambda: None
        p.sync_seek(10.0, time.perf_counter(), 0.0, waiting=cb)
        assert seen == [cb], seen
    finally:
        players.vlc, players.SLOW_START = saved
    print("slow first start: waited out and reported; a dead film fails at "
          "once; an ended film restarts")


def embedded_anchor():
    """Every load hands libvlc our drawable again and turns its own
    fullscreen off - a guard only, as real libvlc keeps both across loads
    already. And once a drawable is set, set_fullscreen reports that it
    does nothing rather than pretend.

    The drawable also sets who gets the mouse and the keys. Windows: both
    off, so libvlc's vout is created disabled and clicks fall through to
    our frame. macOS: mouse on, keys off - the one setting both Mac probes
    agree on, where clicks, the wheel, Space and F all reach Tk."""
    saved = players.vlc, players._tk_nsview
    try:
        # Windows: the hwnd is given to the constructor
        mp = AnchorMP()
        players.vlc = types.SimpleNamespace(
            Instance=lambda *args: AnchorInstance(mp),
            EventType=FAKE_VLC.EventType, State=FAKE_VLC.State)
        p = players.EmbeddedPlayer(0x1234)
        assert mp.calls == [("set_hwnd", 0x1234), ("mouse_input", False),
                            ("key_input", False)], mp.calls
        for film in ("one.mkv", "two.mkv"):
            mp.calls.clear()
            p.load(film)
            assert mp.calls == [("set_media", ("media", film)),
                                ("set_hwnd", 0x1234),
                                ("set_fullscreen", False)], mp.calls
        mp.calls.clear()
        assert p.set_fullscreen(True) is False
        assert mp.calls == [], mp.calls
        print("embedded, Windows: mouse and key input off; every load "
              "re-anchors the hwnd, libvlc fullscreen off; set_fullscreen "
              "reports a no-op")

        # macOS: the NSView arrives later, through attach_tk
        mp = AnchorMP()
        p = players.EmbeddedPlayer._around(AnchorInstance(mp), mp)
        players._tk_nsview = lambda wid: 0xD0A
        assert p.attach_tk(42) is True
        assert mp.calls == [("set_nsobject", 0xD0A), ("mouse_input", True),
                            ("key_input", False)], mp.calls
        mp.calls.clear()
        assert p.attach_tk(42) is True and mp.calls == [], mp.calls   # once
        p.load("one.mkv")
        assert mp.calls == [("set_media", ("media", "one.mkv")),
                            ("set_nsobject", 0xD0A),
                            ("set_fullscreen", False)], mp.calls
        mp.calls.clear()
        assert p.set_fullscreen(True) is False
        assert mp.calls == [], mp.calls
        print("embedded, macOS: attach_tk turns mouse input on and key "
              "input off; every load re-anchors the NSView too")

        # no drawable: the window is libvlc's own, and so is fullscreen
        mp = AnchorMP()
        p = players.EmbeddedPlayer._around(AnchorInstance(mp), mp)
        p.load("one.mkv")
        assert mp.calls == [("set_media", ("media", "one.mkv"))], mp.calls
        assert p.set_fullscreen(True) is True
        assert mp.calls[-1] == ("set_fullscreen", True), mp.calls
        print("embedded, no drawable: load leaves it alone, fullscreen is "
              "libvlc's")
    finally:
        players.vlc, players._tk_nsview = saved


class VolumeMP(AnchorMP):
    """libvlc's volume, mute and subtitle-track calls, as recorded state."""

    def __init__(self):
        super().__init__()
        self.vol, self.muted, self.spu = -1, True, 3

    def audio_get_volume(self):
        return self.vol

    def audio_set_volume(self, v):
        self.vol = v

    def audio_set_mute(self, flag):
        self.muted = flag

    def video_get_spu(self):
        if self.spu is None:
            raise OSError("no vout yet")
        return self.spu


def embedded_volume():
    """The video window's wheel and menu read and step the volume; it is
    clamped to libvlc's 0-125 and never touches mute."""
    mp = VolumeMP()
    p = players.EmbeddedPlayer._around(None, mp)
    assert p.volume() is None            # no audio output yet: -1
    for asked, kept in ((130, 125), (-5, 0), (87.6, 87)):
        p.set_volume(asked)
        assert mp.vol == kept and p.volume() == kept, (asked, mp.vol)
    assert mp.muted is True
    assert p.current_subtitle() == 3
    mp.spu = None
    assert p.current_subtitle() == -1
    print("embedded: volume clamped to 0-125, mute untouched; current "
          "subtitle read, -1 when libvlc can't say")


class FpsMP(AnchorMP):
    def __init__(self, fps):
        super().__init__()
        self.fps = fps

    def get_fps(self):
        if isinstance(self.fps, Exception):
            raise self.fps
        return self.fps


def embedded_fps():
    """The fullscreen bar's +-1 frame steps by the film's frame rate:
    libvlc's, while it reports one (0 until a picture is out)."""
    for given, want in ((23.976, 23.976), (25, 25.0), (0, None), (None, None),
                        (-1.0, None), (OSError("no vout"), None)):
        p = players.EmbeddedPlayer._around(None, FpsMP(given))
        got = p.fps()
        assert got == want and (got is None or isinstance(got, float)), \
            (given, got)
    print("embedded: fps() is libvlc's frame rate, None while it reports 0 "
          "or fails")


def paused_nudge():
    """A paused film steps exactly. The loop's seek allows for a stall
    that a paused film does not have - a one-frame step back landed 0.06 s
    ahead - so paused, a nudge is a plain seek from where the last one put
    it, never from a get_time() that may not have caught up yet."""
    p, mp = make(stall=0.1, seed=3)
    p.pause()
    time.sleep(0.1)
    t = mp.get_time() / 1000            # paused: libvlc's own reading
    assert t > 0.5 and not mp.playing, (t, mp.playing)
    sent = []
    real_set_time = mp.set_time

    def set_time(ms):
        sent.append(ms)
        real_set_time(ms)

    mp.set_time = set_time
    frame = 1 / 25
    p.nudge(-frame)
    assert sent == [round((t - frame) * 1000)], (sent, t)
    assert not p._working
    # libvlc's get_time() is stale for a moment after a seek; two quick
    # presses must still land one frame and two frames on
    mp.get_time = lambda: sent[0]
    start = p._paused_at
    p.nudge(+frame)
    p.nudge(+frame)
    assert sent[1:] == [round((start + frame) * 1000),
                        round((start + 2 * frame) * 1000)], (sent, start)
    assert abs(p._paused_at - (t + frame)) < 0.002, (p._paused_at, t)
    time.sleep(0.5)
    assert not p._working and not mp.playing
    assert abs(mp.truth() - (t + frame)) < 0.002, (mp.truth(), t)
    print(f"paused nudge: -1 frame, +1, +1 from {t:.3f} s sent exact "
          f"seeks {[ms / 1000 for ms in sent]}, no correction thread, and "
          f"the film stayed paused at {mp.truth():.3f} s")

    # playing again, a nudge goes through the loop as before, and a later
    # pause steps from where playback is, not from the old paused step
    del mp.get_time
    p.toggle_pause()
    assert p._paused_at is None
    time.sleep(1.0)
    before = len(sent)
    p.nudge(0.1)
    assert p._working
    wait_done(p)
    p.pause()
    time.sleep(0.1)
    t = mp.get_time() / 1000
    p.nudge(-frame)
    assert sent[-1] == round((t - frame) * 1000), (sent[before:], t)
    print("playing, a nudge is the loop's again; paused later, the step "
          "starts from the film's own position")

    # nothing to step before a film has started, and never before 0
    p.seek(0.01)
    p.nudge(-frame)
    assert sent[-1] == 0 and p._paused_at == 0.0, sent[-2:]
    mp.stop()
    q = players.EmbeddedPlayer._around(None, AnchorMP())
    q.mp.is_playing = lambda: False
    q.mp.get_time = lambda: -1
    q.mp.set_time = lambda ms: sent.append(("never", ms))
    q.nudge(+frame)
    assert not any(isinstance(ms, tuple) for ms in sent), sent
    assert q._paused_at is None
    print("paused nudge: clamped at 0; nothing at all before playback")


def main():
    # 1. the clock: dated position vs truth, despite stale, late events
    p, mp = make()
    errs = []
    for _ in range(10):
        time.sleep(0.37)
        clk = p.clock()
        if clk:
            errs.append(abs(clk[0] - mp.truth(clk[1])))
    stale = abs(mp.get_time() / 1000 - mp.truth())
    print(f"clock: worst error {1000 * max(errs):.1f} ms over {len(errs)} reads "
          f"(raw get_time() is {1000 * stale:.0f} ms off right now)")
    assert len(errs) >= 8 and max(errs) < 0.01, errs

    # 2. syncs onto a moving target, through ~300 ms seek stalls: each
    # lands, and the stall guess (100 ms to start) learns the real one
    for target in (300.0, 420.0, 150.0):
        c = time.perf_counter()
        p.sync_seek(target, c, 0.0)
        wait_done(p)
        time.sleep(0.3)
        now = time.perf_counter()
        err = mp.truth(now) - (target + now - c)
        print(f"sync to {target:.0f} s through a ~300 ms stall: error "
              f"{1000 * err:+.0f} ms, stall guess now {1000 * p._stall:.0f} ms")
        assert abs(err) < 0.03, err
    assert 0.22 < p._stall < 0.38, p._stall
    mp.stop()

    # 3. a nudge moves the picture by what it says
    p, mp = make(stall=0.1, seed=2)
    now = time.perf_counter()
    before = mp.truth(now)
    p.nudge(0.1)
    wait_done(p)
    time.sleep(0.3)
    later = time.perf_counter()
    net = mp.truth(later) - (before + (later - now))
    print(f"nudge +100 ms: net {1000 * net:+.0f} ms, no seek "
          f"(set_time calls: {sum(1 for s in mp.segments if s[2] == 0.0)})")
    assert abs(net - 0.1) < 0.02, net

    # 4. pausing mid-trim puts the rate back
    p.nudge(-0.3)
    time.sleep(0.5)
    assert p._trimming
    p.pause()
    assert not p._trimming and not p._working
    time.sleep(1.2)
    assert mp._rate() == 1.0, mp._rate()
    print("pause during a trim: correction cancelled, rate back to 1x")
    mp.stop()

    # 4b. a slow first start is waited out, a dead film is not
    slow_start()

    # 5. external VLC: a second film, a fresh spawn, a Tk path
    external_load()

    # 5b. external VLC: the window's reads never wait on HTTP
    external_cached_status()

    # 6. embedded: the drawable is re-anchored on every load
    embedded_anchor()

    # 7. embedded: volume and subtitle reads for the video window's menu
    embedded_volume()

    # 8. embedded: the frame rate the +-1 frame buttons step by
    embedded_fps()

    # 9. embedded: a paused nudge is an exact seek
    paused_nudge()
    print("PLAYERS TEST PASSED")


if __name__ == "__main__":
    main()
