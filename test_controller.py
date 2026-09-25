"""SyncController decisions, driven against a simulated stream.

No window, no sound card, no film: a fake stream plays (or pauses) on a
fake clock, a fake player can be seeked and nudged, and the matcher is
replaced by one that "hears" the stream exactly when its audio is
audible, inside the search window and in the film searched. What is
tested is the controller's judgement - when to seek, how far, and when
to leave playback alone:

- auto mode leaves a nudged film alone (it once "corrected" any nudge over
  0.35 s at every check, forever);
- real drift is corrected, to the right place;
- a weak match on Resync does not move the film; the very first sync
  still uses its best guess;
- a weak first look listens longer before giving up;
- a pause auto mode made survives a manual sync that was not applied
  (the film once stayed paused for good), and is handed back when the
  interruption plays the film, loads another or switches player;
- a false pause (a stretch the matcher cannot hear while the stream keeps
  playing) recovers, because the resume search grows with time - in
  steps, and only so far, so a long real pause does not decode ever more
  of the film at every look;
- a film that cannot be read (its drive unplugged) is not taken for a
  paused stream: the same failure three times running is reported once
  and switches auto mode off - whether the film was playing or auto mode
  had paused it and was waiting for the stream - while a silent capture
  is still a pause;
- nudges during a watch party go to the session, which would otherwise
  undo them, and a session that takes the playhead while auto mode is
  listening is not overruled by what auto mode then finds;
- leaving a session, or quitting, does not wait on the relay (the UI
  thread froze for the websocket's closing handshake), though quitting
  gives a goodbye still going out - even one from a Leave just before -
  a moment before the player stops;
- the time readout carries its rounding (119.96 s once read "1:60.0").
"""

import queue
import threading
import time
import types

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
        self.film = "film.mkv"    # what the streamer is playing
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

    def load(self, path):
        self.path = path          # another film: stopped, nowhere yet
        self.anchor, self.playing = None, False

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
        self.on_capture = None    # runs while "recording": the world moves on

    def running(self):
        return True

    def mark(self):
        return self.clock()

    def capture_span(self, seconds, start=None):
        if self.on_capture:
            self.on_capture()
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

    # scores relative to the matcher's own gates, whatever scale they use
    S, Z = controller.audio_matcher.SCORE_OK, controller.audio_matcher.Z_OK

    def find(path, feats, lo=None, hi=None, progress=None):
        t0, seconds = feats
        true = stream.pos(t0)
        inside = (lo is None or lo <= true) and (hi is None or true <= hi)
        if (path != stream.film or not stream.audible or not stream.playing
                or not inside):
            return (lo or 0.0) + 7.0, 0.2 * S, 0.6 * Z    # noise
        if seconds < stream.weak_until:
            return true, 0.4 * S, Z                       # right but weak
        return true, 3.0 * S, 2.5 * Z
    controller.audio_matcher.prep_capture = prep
    controller.audio_matcher.find_match_audio = find


def failing_matcher(errors):
    """Make each look at the film raise the next of `errors` (None: hear
    the stream as usual). Returns the ones not yet raised."""
    hear = controller.audio_matcher.find_match_audio
    todo = list(errors)

    def find(path, feats, lo=None, hi=None, progress=None):
        e = todo.pop(0) if todo else None
        if e is not None:
            raise e
        return hear(path, feats, lo, hi, progress)
    controller.audio_matcher.find_match_audio = find
    return todo


class Log:
    """Stands in for diagnostics: what would have gone to the user's log."""
    def __init__(self):
        self.lines = []
        self.blocks = []          # (title, text), e.g. a traceback

    def log(self, message):
        self.lines.append(message)

    def log_block(self, title, text):
        self.blocks.append((title, text))


def make(offset=0.0):
    clock = Clock()
    stream = Stream(clock)
    install_fakes(stream)
    controller.diagnostics = Log()
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


def press(ctl, fn, *args):
    """A sync button pressed: claimed now, as the UI thread does, with its
    worker handed back to run later - so an auto loop in between sees the
    sync running."""
    workers = []
    real = threading.Thread

    class Deferred:
        def __init__(self, target, args=(), daemon=None):
            workers.append(lambda: target(*args))

        def start(self):
            pass
    controller.threading.Thread = Deferred
    try:
        assert fn(*args), "the sync did not start"
    finally:
        controller.threading.Thread = real
    return workers[0]


def run_loop(ctl, clock, seconds, events=()):
    """Run the real _auto_loop, on this thread, for `seconds` of simulated
    time. Its sleeps advance the clock; `events` are (after, fn) pairs run
    once `after` seconds have passed, so the loop meets a button press or
    a session start between its passes, as it would live."""
    start = clock.now
    todo = sorted(events, key=lambda e: e[0])

    def sleep(dt):
        clock.now += dt
        while todo and clock.now - start >= todo[0][0]:
            todo.pop(0)[1]()
        if clock.now - start >= seconds:
            ctl._closing = True
    real = controller.time
    controller.time = types.SimpleNamespace(sleep=sleep, monotonic=clock)
    ctl._closing = False
    try:
        ctl._auto_loop()
    finally:
        controller.time = real


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get())
    return out


def statuses(q):
    return [item[1] for item in drain(q) if item[0] == "status"]


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


def test_long_pause_window_bounded():
    ctl, stream, player, clock, q = make()
    run_sync(ctl, ctl.sync, "", "", "audio")
    windows = []
    hear = controller.audio_matcher.find_match_audio

    def spy(path, feats, lo=None, hi=None, progress=None):
        windows.append((lo, hi))
        return hear(path, feats, lo, hi, progress)
    controller.audio_matcher.find_match_audio = spy
    state = new_state()
    stream.pause()                    # the streamer steps away for an hour
    for _ in range(3):
        clock.now += ctl.auto_step(state)
    assert state["mode"] == "probe" and not player.playing
    del windows[:]
    start = clock.now
    while clock.now - start < 3600:
        clock.now += ctl.auto_step(state)
    shapes = set(windows)
    assert len(shapes) <= 12, \
        f"{len(windows)} looks asked for {len(shapes)} different windows"
    reach = max(hi for lo, hi in windows) - state["pause_point"]
    limit = controller.PAUSE_LOOK_AHEAD + controller.PAUSE_GROW_MAX
    assert reach <= limit, f"an hour in, the search reaches {reach:.0f} s ahead"
    stream.play()
    clock.now += ctl.auto_step(state)
    assert state["mode"] == "normal" and abs(player.pos() - stream.pos()) < 1e-6
    print(f"long pause: {len(windows)} looks in an hour used {len(shapes)} "
          f"windows reaching {reach:.0f} s ahead, then resumed in place")


def test_auto_pause_survives_weak_resync():
    ctl, stream, player, clock, q = make()
    ctl.auto_enabled = True
    run_sync(ctl, ctl.sync, "", "", "audio")
    seen = {}
    sync = []

    def paused():
        assert not player.playing, "auto mode did not follow the pause"
        seen["seeks"] = len(player.seeks)

    def still_paused():
        # the stream is still paused, so the Resync was weak: not applied
        assert any("NOT applied" in m for m in statuses(q))
        assert not player.playing and len(player.seeks) == seen["seeks"]

    def resumed():
        assert player.playing, "the film auto mode paused was never resumed"
        assert abs(player.pos() - stream.pos()) < 1e-6

    def resynced_by_hand():
        sync.pop()()            # the stream is back, so this one applies
        assert player.playing
        seen["seeks"] = len(player.seeks)

    def handed_back():
        assert len(player.seeks) == seen["seeks"], \
            "auto mode 'resumed' a film a manual sync had already resumed"
    run_loop(ctl, clock, 330, [
        (0, stream.pause),
        (60, paused),
        (60, lambda: sync.append(press(ctl, ctl.resync, "", "2:00", "audio"))),
        (70, lambda: sync.pop()()),
        (75, still_paused),
        (100, stream.play),
        (140, resumed),
        # paused again, and this time Resync is pressed as the stream
        # comes back, so it is the manual sync that resumes the film
        (150, stream.pause),
        (230, paused),
        (240, lambda: sync.append(press(ctl, ctl.resync, "", "2:00", "audio"))),
        (240, stream.play),
        (255, resynced_by_hand),
        (330, handed_back)])
    assert player.playing and abs(player.pos() - stream.pos()) < 1e-6
    print("auto pause: kept across a weak Resync and resumed with the "
          "stream; handed back when a Resync resumed it")


def test_held_pause_is_only_that_films():
    # auto mode paused the film for a stream pause; Auto is unticked and
    # something else goes up - another film, or this one on the other
    # player - before it is ticked again and the stream plays. What is up
    # now was never paused by auto mode, so it must not look for the
    # stream in it around the old pause point, nor "resume" it
    def run(swap, on_external=False):
        ctl, stream, player, clock, q = make()
        ctl.auto_enabled = True
        if on_external:
            ctl.external = Player(clock)
            run_sync(ctl, ctl.use_external, True)
        run_sync(ctl, ctl.sync, "", "", "audio")
        was = ctl.player
        looks = []
        hear = controller.audio_matcher.find_match_audio

        def find(path, feats, lo=None, hi=None, progress=None):
            looks.append((path, lo, hi))
            return hear(path, feats, lo, hi, progress)
        controller.audio_matcher.find_match_audio = find
        seen = {}

        def paused():
            assert not was.playing, "auto mode did not follow the pause"
            seen["seeks"] = len(was.seeks)

        def rearm():
            ctl.auto_enabled = True
            del looks[:]
        run_loop(ctl, clock, 200, [
            (0, stream.pause),
            (60, paused),
            (61, lambda: setattr(ctl, "auto_enabled", False)),
            (62, lambda: swap(ctl)),
            (63, rearm),
            (100, stream.play)])
        assert not looks, \
            f"auto mode looked for a pause it did not make: {looks[:2]}"
        now = ctl.player
        assert not now.playing and (now is was or not now.seeks), \
            "auto mode 'resumed' a film it never paused"
        assert not was.playing and len(was.seeks) == seen["seeks"]

    run(lambda ctl: ctl.load_file("other.mkv"))
    run(lambda ctl: ctl.use_embedded(), on_external=True)
    print("auto pause: let go once another film, or the other player, is up")


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


def test_vanished_film_gives_up():
    ctl, stream, player, clock, q = make()
    ctl.auto_enabled = True
    run_sync(ctl, ctl.sync, "", "", "audio")
    drain(q)
    gone = controller.matcher.MatchError(
        "ffmpeg could not read this file as a video.")  # drive unplugged
    failing_matcher([gone] * 100)
    run_loop(ctl, clock, 600)
    got = drain(q)
    offs = [e[1] for e in got if e[0] == "auto_off"]
    assert offs, "auto mode never gave up on a film it could not read"
    assert len(offs) == 1 and "still available" in offs[0], offs
    assert not ctl.auto_enabled
    assert player.playing, "an unreadable film was taken for a stream pause"
    failed = [e for e in got if e[0] == "status" and "check failed" in e[1]]
    assert len(failed) == 1, failed
    log = controller.diagnostics
    assert len(log.blocks) == 1 and "MatchError" in log.blocks[0][1], \
        "want one traceback per failure streak"
    assert len(log.lines) == 2, log.lines     # one repeat, then giving up
    print("vanished film: one report, one traceback, then auto re-sync off")


def test_vanished_paused_film_gives_up():
    # the likelier way to meet it: the film is paused, auto mode waiting
    # for the stream to come back, when its drive goes (a player whose file
    # vanished mid-play tends to stop, and a stopped film is left alone)
    ctl, stream, player, clock, q = make()
    ctl.auto_enabled = True
    run_sync(ctl, ctl.sync, "", "", "audio")
    drain(q)
    hear = controller.audio_matcher.find_match_audio
    gone = []
    seen = {}

    def find(path, feats, lo=None, hi=None, progress=None):
        if gone:
            raise controller.matcher.MatchError(
                "ffmpeg could not read this file as a video.")
        return hear(path, feats, lo, hi, progress)
    controller.audio_matcher.find_match_audio = find

    def unplug():
        assert not player.playing, "auto mode did not follow the pause"
        seen["seeks"] = len(player.seeks)
        gone.append(True)

    def gave_up():
        got = drain(q)
        offs = [e[1] for e in got if e[0] == "auto_off"]
        assert offs, "auto mode kept waiting on a film it could not read"
        assert len(offs) == 1 and "still available" in offs[0], offs
        assert not ctl.auto_enabled
        assert not player.playing and len(player.seeks) == seen["seeks"], \
            "the unreadable film was moved"
        failed = [e for e in got if e[0] == "status" and "check failed" in e[1]]
        assert len(failed) == 1, failed
        log = controller.diagnostics
        assert len(log.blocks) == 1 and "MatchError" in log.blocks[0][1]
        assert len(log.lines) == 2, log.lines

    def replug_and_retick():
        del gone[:]
        ctl.auto_enabled = True
    run_loop(ctl, clock, 330, [
        (0, stream.pause),
        (60, unplug),
        (190, gave_up),
        (200, replug_and_retick),
        (250, stream.play)])
    # the pause was auto mode's, so with the film back it is still its to lift
    assert player.playing and abs(player.pos() - stream.pos()) < 1e-6, \
        "the film auto mode paused was not resumed once Auto was re-ticked"
    assert not any(e[0] == "auto_off" for e in drain(q))
    print("vanished film while paused: auto re-sync off once, the film left "
          "paused; re-ticked, it resumes with the stream")


def test_give_up_needs_a_run():
    a = controller.matcher.MatchError("Could not decode audio from the file "
                                      "in that range.")
    b = OSError("[Errno 2] No such file or directory")

    def run(errors, arm=()):
        """`arm`: (after, on) pairs ticking Auto on or off meanwhile."""
        ctl, stream, player, clock, q = make()
        ctl.auto_enabled = True
        run_sync(ctl, ctl.sync, "", "", "audio")
        drain(q)
        left = failing_matcher(errors)
        run_loop(ctl, clock, 400, [
            (after, lambda on=on: setattr(ctl, "auto_enabled", on))
            for after, on in arm])
        offs = [e[1] for e in drain(q) if e[0] == "auto_off"]
        return ctl, offs, left

    # a clean check in between: a blip each time, never permanent
    ctl, offs, left = run([a, a, None] * 3)
    assert not offs and ctl.auto_enabled and not left, offs
    assert len(controller.diagnostics.blocks) == 3   # one per streak
    # different failures in turn are not the same failure repeating
    ctl, offs, left = run([a, b] * 4)
    assert not offs and ctl.auto_enabled and not left, offs
    # an idle spell (unticked, re-ticked) starts the count over...
    ctl, offs, left = run([a] * 5, [(45, False), (50, True)])
    assert len(offs) == 1 and not left, (offs, left)
    # ...and anything else gives up too, just worded for what it is
    ctl, offs, left = run([ValueError("bad window")] * 3)
    assert len(offs) == 1 and "bad window" in offs[0], offs
    assert "still available" not in offs[0]
    print("give-up: only the same failure three times running, counted "
          "afresh after a clean check or an idle spell")


def test_silent_capture_is_a_miss():
    ctl, stream, player, clock, q = make()
    ctl.auto_enabled = True
    run_sync(ctl, ctl.sync, "", "", "audio")
    drain(q)

    def silence():
        raise RuntimeError("Captured only silence.")
    ctl._loopback().on_capture = silence
    run_loop(ctl, clock, 120)
    got = drain(q)
    assert not player.playing, "a silent stream was not followed as a pause"
    assert not any(e[0] == "auto_off" for e in got)
    assert not any("check failed" in e[1] for e in got if e[0] == "status")
    print("silent capture: a stream pause, not a failure")


def test_viewer_nudge_goes_to_session():
    ctl, stream, player, clock, q = make()
    viewer = session.ViewerSession("ws://x", "CODE", "film.mkv", player, q)
    ctl.session = viewer
    shown = ctl.nudge(0.5)
    assert viewer.offset == 0.5 and ctl.offset == 0.0 and shown == 0.5
    ctl.reset_offset()
    assert viewer.offset == 0.0
    print("watch party: nudges go to the session's offset")


def test_session_start_mid_listen_wins():
    ctl, stream, player, clock, q = make()
    run_sync(ctl, ctl.sync, "", "", "audio")
    mon = ctl._loopback()
    viewer = session.ViewerSession("ws://x", "CODE", "film.mkv", player, q)

    def party_starts():
        ctl.session = viewer

    # drift to correct - but a watch party takes over during the listen
    player.anchor = (player.pos() - 1.3, clock())
    n = len(player.seeks)
    mon.on_capture = party_starts
    clock.now += ctl.auto_step(new_state())
    assert len(player.seeks) == n, "auto mode corrected drift under a session"

    # a resume found while a watch party starts: the party has the film
    ctl.session, mon.on_capture = None, None
    state = new_state()
    stream.pause()
    for _ in range(3):
        clock.now += ctl.auto_step(state)
    assert state["mode"] == "probe" and not player.playing
    stream.play()
    n = len(player.seeks)
    mon.on_capture = party_starts
    clock.now += ctl.auto_step(state)
    assert len(player.seeks) == n and not player.playing, \
        "auto mode resumed the film under a session"
    assert state["mode"] == "probe"

    # the party over, the pause auto mode made is still its to lift
    ctl.session, mon.on_capture = None, None
    clock.now += ctl.auto_step(state)
    assert state["mode"] == "normal" and abs(player.pos() - stream.pos()) < 1e-6
    print("watch party: a session started mid-listen keeps the playhead")


class SlowSession:
    """A session whose stop() hangs, as a websocket closing handshake does
    when the relay has gone away - and whose stop flag only goes up once
    it is done, so a caller that relies on stop() for it is caught."""
    def __init__(self, hang=5.0, order=None, name="session"):
        self.stop_flag = threading.Event()
        self.hang = hang
        self.order = order if order is not None else []
        self.name = name
        self.called = threading.Event()

    def stop(self):
        self.called.set()
        time.sleep(self.hang)
        self.stop_flag.set()
        self.order.append(self.name)


def test_session_teardown_does_not_block():
    ctl, stream, player, clock, q = make()
    sess = ctl.session = SlowSession()
    t = time.perf_counter()
    ctl.leave()
    took = time.perf_counter() - t
    assert took < 0.5, f"Leave blocked the UI thread for {took:.1f} s"
    assert sess.stop_flag.is_set() and not ctl.session_running(), \
        "the session was still running when Leave returned"
    assert sess.called.wait(2), "the session was never stopped"

    ctl, stream, player, clock, q = make()
    sess = ctl.session = SlowSession()
    t = time.perf_counter()
    ctl.close()
    took = time.perf_counter() - t
    assert took < controller.SESSION_CLOSE_WAIT + 0.5, \
        f"quitting waited {took:.1f} s on the relay"
    assert sess.stop_flag.is_set(), "the session was still running at quit"

    # a relay that answers still gets its goodbye before the player stops
    order = []
    ctl, stream, player, clock, q = make()
    ctl.session = SlowSession(hang=0.2, order=order)
    player.stop = lambda: order.append("player")
    ctl.close()
    assert order == ["session", "player"], order

    # ...and so does one from a Leave just before quitting, which once
    # found no session at quit and stopped the player under the goodbye
    order = []
    ctl, stream, player, clock, q = make()
    ctl.session = SlowSession(hang=0.3, order=order)
    player.stop = lambda: order.append("player")
    ctl.leave()
    ctl.close()
    assert order == ["session", "player"], \
        f"quitting after Leave stopped the player first: {order}"

    # Leave, join another party, quit: both goodbyes go out first...
    order = []
    ctl, stream, player, clock, q = make()
    ctl.session = SlowSession(hang=0.6, order=order, name="left")
    player.stop = lambda: order.append("player")
    ctl.leave()
    ctl.session = SlowSession(hang=0.1, order=order, name="joined")
    ctl.close()
    assert order == ["joined", "left", "player"], order

    # ...but two dead relays still hold quitting SESSION_CLOSE_WAIT in all
    ctl, stream, player, clock, q = make()
    ctl.session = SlowSession()
    ctl.leave()
    ctl.session = SlowSession()
    t = time.perf_counter()
    ctl.close()
    took = time.perf_counter() - t
    assert took < controller.SESSION_CLOSE_WAIT + 0.5, \
        f"quitting after Leave waited {took:.1f} s on the relays"
    print("sessions: Leave returns at once, quitting waits "
          f"{controller.SESSION_CLOSE_WAIT:.1f} s at most - for a goodbye "
          "from a Leave just before, too")


def test_fmt_time_carries():
    fmt = controller.fmt_time
    cases = {0: "0:00.0", 59.94: "0:59.9", 59.96: "1:00.0",
             119.96: "2:00.0", 3599.96: "1:00:00.0", 3661.2: "1:01:01.2",
             -3.0: "0:00.0"}
    for s, want in cases.items():
        assert fmt(s) == want, f"fmt_time({s}) = {fmt(s)!r}, want {want!r}"
    for m in range(1, 150):              # every minute mark to 2.5 h
        for k in range(-10, 10):
            s = m * 60 + k / 100
            out = fmt(s)
            *hm, sec = out.split(":")
            assert float(sec) < 60 and all(int(p) < 60 for p in hm[1:]), \
                f"fmt_time({s}) = {out!r}"
            assert abs(controller.parse_time(out) - s) <= 0.05 + 1e-9, out
    print("clock: 119.96 s reads 2:00.0, and no minute mark shows :60")


def main():
    test_nudged_film_left_alone()
    test_drift_corrected()
    test_weak_resync_not_applied()
    test_weak_first_look_listens_longer()
    test_false_pause_recovers()
    test_real_pause_and_resume()
    test_auto_pause_survives_weak_resync()
    test_held_pause_is_only_that_films()
    test_long_pause_window_bounded()
    test_vanished_film_gives_up()
    test_vanished_paused_film_gives_up()
    test_give_up_needs_a_run()
    test_silent_capture_is_a_miss()
    test_viewer_nudge_goes_to_session()
    test_session_start_mid_listen_wins()
    test_session_teardown_does_not_block()
    test_fmt_time_carries()
    print("CONTROLLER TEST PASSED")


if __name__ == "__main__":
    main()
