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
"""

import random
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
    print("PLAYERS TEST PASSED")


if __name__ == "__main__":
    main()
