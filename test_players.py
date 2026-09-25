"""The closed-loop player, against a simulated libvlc.

The simulation reproduces what was measured on real libvlc 3 (1080p,
ground truth read off the displayed frames):
- get_time() changes only every 250-500 ms, exact at the change, then
  stale; the change event is occasionally delivered 50-150 ms late;
- set_time() echoes its target at once, then playback stalls for a while
  before it restarts - an amount that varies per seek;
- set_rate() takes effect about 1 s late, and so does going back to 1x.

On real libvlc the same code measured: sync error 5-9 ms median (was
82-179 ms), position estimate within 1 ms, +0.1 s nudge nets +96 ms (a
seek-based nudge netted about -170 ms). This test holds the logic to
that without needing VLC installed.

It also pins what loading a film sends external VLC - its HTTP commands
and its command line - with a fake that records them.
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
            self.segments.append((now + stall, ms / 1000.0, self._rate()))

    def set_rate(self, r):
        with self.lock:
            self.rates.append((time.perf_counter() + 1.0, r))       # ~1 s late

    def set_pause(self, flag):
        now = time.perf_counter()
        self._set_segment(now, self.truth(now), 0.0 if flag else self._rate())
        self.playing = not flag

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
                                NothingSpecial="none"))


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

    # 5. external VLC: a second film, a fresh spawn, a Tk path
    external_load()
    print("PLAYERS TEST PASSED")


if __name__ == "__main__":
    main()
