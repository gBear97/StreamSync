"""SyncController decisions, driven against a simulated stream.

No window, no sound card, no film: a fake stream plays (or pauses) on a
fake clock, a fake player can be seeked and nudged, and the matcher is
replaced by one that "hears" the stream exactly when its audio is
audible and inside the search window. What is tested is the controller's
judgement - when to seek, how far, and when to leave playback alone:

- auto mode leaves a nudged film alone (it once "corrected" any nudge over
  0.35 s at every check, forever);
- real drift is corrected, to the right place;
- a weak match on Resync does not move the film; the very first sync
  still uses its best guess;
- a weak first look listens longer before giving up;
- a false pause (a stretch the matcher cannot hear while the stream keeps
  playing) recovers, because the resume search grows with time;
- nudges during a watch party go to the session, which would otherwise
  undo them.
"""

import queue
import threading

import controller
import session


class Clock:
    """Simulated perf_counter, advanced by hand."""
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Stream:
    """The live stream: film position as a function of time."""
    def __init__(self, clock, pos=600.0):
        self.clock = clock
        self.anchor = (pos, clock())
        self.playing = True
        self.audible = True       # can the matcher hear the film at all?
        self.weak_until = 0.0     # recordings shorter than this are weak

    def pos(self, at=None):
        at = self.clock() if at is None else at
        p, t = self.anchor
        return p + (at - t if self.playing else 0.0)

    def pause(self):
        self.anchor = (self.pos(), self.clock())
        self.playing = False

    def play(self):
        self.anchor = (self.pos(), self.clock())
        self.playing = True


class Player:
    def __init__(self, clock):
        self.now = clock
        self.anchor = None        # (pos, at) once started
        self.playing = False
        self.seeks = []
        self.nudges = []

    def pos(self):
        if self.anchor is None:
            return None
        p, t = self.anchor
        return p + (self.now() - t if self.playing else 0.0)

    def time(self):
        return self.pos()

    def clock(self):
        if not self.playing or self.anchor is None:
            return None
        return self.pos(), self.now()

    def is_playing(self):
        return self.playing

    def pause(self):
        if self.playing:
            self.anchor = (self.pos(), self.now())
            self.playing = False

    def sync_seek(self, match_t, t0, offset):
        target = match_t + (self.now() - t0) + offset
        self.seeks.append(target)
        self.anchor = (target, self.now())
        self.playing = True

    def nudge(self, delta):
        self.nudges.append(delta)
        if self.anchor is not None:
            self.anchor = (self.pos() + delta, self.now())

    def set_mute(self, mute):
        pass

    def stop(self):
        pass


class Monitor:
    """Hands out 'recordings' that are just (start time, length)."""
    def __init__(self, clock):
        self.clock = clock
        self.frame = 0

    def running(self):
        return True

    def mark(self):
        return self.clock()

    def capture_span(self, seconds, start=None):
        t0 = self.clock() - seconds if start is None else start
        # recording takes real (simulated) time when it must wait
        self.clock.now = max(self.clock.now, t0 + seconds)
        return (t0, seconds), 48000, t0, t0

    def capture(self, seconds, start=None):
        return self.capture_span(seconds, start)[:3]

    def stop(self):
        pass


def install_fakes(stream):
    def prep(samples, sr):
        return samples

    def find(path, feats, lo=None, hi=None, progress=None):
        t0, seconds = feats
        true = stream.pos(t0)
        inside = (lo is None or lo <= true) and (hi is None or true <= hi)
        if not stream.audible or not stream.playing or not inside:
            return (lo or 0.0) + 7.0, 0.05, 3.0          # noise
        if seconds < stream.weak_until:
            return true, 0.08, 5.0                        # right but weak
        return true, 0.6, 15.0
    controller.audio_matcher.prep_capture = prep
    controller.audio_matcher.find_match_audio = find


def make(offset=0.0):
    clock = Clock()
    stream = Stream(clock)
    install_fakes(stream)
    player = Player(clock)
    q = queue.Queue()
    ctl = controller.SyncController(q, player,
                                    monitor_factory=lambda name: Monitor(clock))
    ctl.video_path = "film.mkv"
    ctl.offset = offset
    ctl._now = clock
    ctl._log = lambda *a, **k: None
    return ctl, stream, player, clock, q


def new_state():
    return {"mode": "normal", "failures": 0, "pause_point": None,
            "paused_at": None}


def run_sync(ctl, fn, *args):
    """Run a manual sync's worker inline instead of on a thread."""
    real = threading.Thread

    class Inline:
        def __init__(self, target, args=(), daemon=None):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)
    controller.threading.Thread = Inline
    try:
        return fn(*args)
    finally:
        controller.threading.Thread = real


def statuses(q):
    out = []
    while not q.empty():
        item = q.get()
        if item[0] == "status":
            out.append(item[1])
    return out


def test_nudged_film_left_alone():
    ctl, stream, player, clock, q = make(offset=0.5)
    run_sync(ctl, ctl.sync, "", "", "audio")
    assert abs(player.pos() - (stream.pos() + 0.5)) < 1e-6, "first sync missed"
    n = len(player.seeks)
    state = new_state()
    for _ in range(5):
        clock.now += ctl.auto_step(state)
    assert len(player.seeks) == n, \
        f"auto mode re-seeked a nudged film {len(player.seeks) - n} times"
    print("auto mode: a +0.5 s nudge is kept, not 'corrected'")


def test_drift_corrected():
    ctl, stream, player, clock, q = make(offset=0.2)
    run_sync(ctl, ctl.sync, "", "", "audio")
    player.anchor = (player.pos() - 1.3, clock())     # the film slipped
    state = new_state()
    clock.now += ctl.auto_step(state)
    err = player.pos() - (stream.pos() + 0.2)
    assert abs(err) < 1e-6, f"drift not corrected to the offset: {err:+.3f}"
    assert any("corrected -1.30s" in s or "corrected +1.30s" in s for s in statuses(q))
    print("auto mode: 1.3 s of drift corrected onto stream + offset")


def test_weak_resync_not_applied():
    ctl, stream, player, clock, q = make()
    run_sync(ctl, ctl.sync, "", "", "audio")
    stream.audible = False                       # every look comes back weak
    run_sync(ctl, ctl.resync, "", "2:00", "audio")
    assert len(player.seeks) == 1, "a weak Resync moved the film"
    assert any("NOT applied" in s for s in statuses(q))

    ctl, stream, player, clock, q = make()
    stream.audible = False
    run_sync(ctl, ctl.sync, "", "", "audio")
    assert len(player.seeks) == 1, "first sync should still use its best guess"
    print("weak match: Resync leaves the film alone; first sync still guesses")


def test_weak_first_look_listens_longer():
    ctl, stream, player, clock, q = make()
    stream.weak_until = 10.0                     # 6 s is not enough, 12 s is
    run_sync(ctl, ctl.sync, "", "", "audio")
    msgs = statuses(q)
    assert any("listening longer (12 s)" in s for s in msgs), msgs
    assert abs(player.pos() - stream.pos()) < 1e-6
    print("weak first look: extended to 12 s and matched")


def test_false_pause_recovers():
    ctl, stream, player, clock, q = make()
    ctl.auto_enabled = True
    run_sync(ctl, ctl.sync, "", "", "audio")
    state = new_state()
    stream.audible = False            # a long stretch the matcher can't hear...
    for _ in range(3):
        clock.now += ctl.auto_step(state)
    assert state["mode"] == "probe" and not player.playing, "pause not followed"
    for _ in range(12):               # ...while the stream keeps playing
        clock.now += ctl.auto_step(state)
    assert stream.pos() > state["pause_point"] + controller.PAUSE_LOOK_AHEAD, \
        "test setup: the stream should have left the old fixed window"
    stream.audible = True
    for _ in range(3):
        clock.now += ctl.auto_step(state)
        if state["mode"] == "normal":
            break
    assert state["mode"] == "normal", "never recovered from a false pause"
    assert abs(player.pos() - stream.pos()) < 1e-6
    print("false pause: resume search grew with time and found the stream "
          f"{stream.pos() - state['pause_point']:.0f} s past the pause point")


def test_real_pause_and_resume():
    ctl, stream, player, clock, q = make()
    run_sync(ctl, ctl.sync, "", "", "audio")
    state = new_state()
    stream.pause()
    for _ in range(3):
        clock.now += ctl.auto_step(state)
    assert state["mode"] == "probe" and not player.playing
    clock.now += 30
    stream.play()
    clock.now += ctl.auto_step(state)
    assert state["mode"] == "normal" and abs(player.pos() - stream.pos()) < 1e-6
    print("real pause: followed, then resumed in place")


def test_viewer_nudge_goes_to_session():
    ctl, stream, player, clock, q = make()
    viewer = session.ViewerSession("ws://x", "CODE", "film.mkv", player, q)
    ctl.session = viewer
    shown = ctl.nudge(0.5)
    assert viewer.offset == 0.5 and ctl.offset == 0.0 and shown == 0.5
    ctl.reset_offset()
    assert viewer.offset == 0.0
    print("watch party: nudges go to the session's offset")


def main():
    test_nudged_film_left_alone()
    test_drift_corrected()
    test_weak_resync_not_applied()
    test_weak_first_look_listens_longer()
    test_false_pause_recovers()
    test_real_pause_and_resume()
    test_viewer_nudge_goes_to_session()
    print("CONTROLLER TEST PASSED")


if __name__ == "__main__":
    main()
