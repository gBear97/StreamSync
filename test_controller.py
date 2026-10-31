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
- auto mode checks every interval, timed from the start of each pass,
  and no longer pauses the film: a silent stream is a miss, tried again;
- a film that cannot be read (its drive unplugged) is not taken for a
  paused stream: the same failure three times running is reported once
  and switches auto mode off, while failures that differ, in type or in
  message, are not a run;
- Pause with the stream (the watcher) waits for a sync; pauses the film
  when the stream stops carrying its sound and plays it from the exact
  spot when it comes back; leaves a quiet stretch of film alone; under
  talk-over, pauses as each rule says, and recovers from a false pause
  where the stream really is; obeys a hand on Play until it hears the
  film again; corrects a steady drift; finds a stream that comes back far
  away with a wide search; keeps its pause across a weak Resync, lets it
  go for another film, stands aside for a watch party; and its switch
  and rule are saved, the old Follow setting carried over;
- nudges during a watch party go to the session, which would otherwise
  undo them, and a session that takes the playhead while auto mode is
  listening is not overruled by what auto mode then finds;
- leaving a session, or quitting, does not wait on the relay (the UI
  thread froze for the websocket's closing handshake), though quitting
  gives a goodbye still going out - even one from a Leave just before -
  a moment before the player stops;
- the time readout carries its rounding (119.96 s once read "1:60.0");
- switching back from external VLC hands the built-in player a film
  opened meanwhile, and leaves one it already holds where it is;
- every decided match tells the shell what happened, once ("synced": a
  manual sync trusted or weak, applied or not; an auto check only when it
  could measure the drift; a resume), and a sync that failed says nothing;
- a film the built-in player could not load is not named as loaded.
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
        self.coarse = 0.0         # how far out the matcher's answer is (its steps)
        self.unclear = 0          # refinements that come back unclear before one doesn't
        self.quiet = False        # the film's track is quiet here: nothing to listen for
        self.talk_ncc = 0.2       # what a look scores while talk-over hides the film
        self.echo = False         # a recording that began before a pause hears the film in it
        self.paused_since = None

    def pos(self, at=None):
        at = self.clock() if at is None else at
        p, t = self.anchor
        return p + (at - t if self.playing else 0.0)

    def pause(self):
        self.anchor = (self.pos(), self.clock())
        self.playing = False
        self.paused_since = self.clock()

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
        self.slow_start = False      # sync_seek reports a slow start

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

    def sync_seek(self, match_t, t0, offset, waiting=None):
        if waiting is not None and self.slow_start:
            waiting()
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

    def settling(self):
        return False


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

    def capture_span(self, seconds, start=None, **_kw):
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
        # with echo on, a recording that began before a pause still holds
        # the film it heard then, and matches it there
        heard = stream.playing or (stream.echo and stream.paused_since is not None
                                   and t0 < stream.paused_since)
        if (path != stream.film or not stream.audible or not heard
                or not inside):
            return (lo or 0.0) + 7.0, 0.2 * S, 0.6 * Z    # noise
        if seconds < stream.weak_until:
            return true + stream.coarse, 0.4 * S, Z       # right but weak
        return true + stream.coarse, 3.0 * S, 2.5 * Z

    def refine(samples, sr, path, t, search=controller.audio_matcher.REFINE_SEARCH):
        t0, seconds = samples
        true = stream.pos(t0)
        if (path != stream.film or not stream.audible or not stream.playing
                or abs(true - t) > search):
            return None
        if stream.unclear:
            stream.unclear -= 1
            return None
        return true
    def presence(path, samples, sr, at, lo=-controller.audio_matcher.PRESENCE_SEARCH,
                 hi=controller.audio_matcher.PRESENCE_SEARCH,
                 heard=controller.audio_matcher.PRESENCE_HEARD,
                 missing=controller.audio_matcher.PRESENCE_MISSING):
        # a look at the stream against the film where it should be: heard
        # where the stream carries that stretch of film, missing where it
        # carries none (paused, another film), quiet where the film has
        # nothing to hear, and talk_ncc while talk-over hides it
        t0, seconds = samples
        P = controller.audio_matcher.Presence
        if stream.quiet:
            return P("quiet", None, 0.0, -40.0, 0.0)
        if path != stream.film or not stream.playing:
            return P("missing", None, 0.05, 0.0, 1.0)
        if not stream.audible:
            ncc = stream.talk_ncc
            state = "heard" if ncc >= heard else ("missing" if ncc < missing else "unsure")
            return P(state, 0.0, ncc, 0.0, 1.0)
        shift = stream.pos(t0) - at
        if lo <= shift <= hi:
            return P("heard", shift, 0.8, 0.0, 1.0)
        return P("missing", None, 0.05, 0.0, 1.0)
    controller.audio_matcher.prep_capture = prep
    controller.audio_matcher.find_match_audio = find
    controller.audio_matcher.refine_match = refine
    controller.audio_matcher.film_presence = presence
    controller.audio_matcher.presence_cached = lambda path, start: True


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
    return {"failures": 0, "fail_sig": None, "fail_n": 0}


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
    run_sync(ctl, ctl.sync, "")
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
    run_sync(ctl, ctl.sync, "")
    player.anchor = (player.pos() - 1.3, clock())     # the film slipped
    state = new_state()
    clock.now += ctl.auto_step(state)
    err = player.pos() - (stream.pos() + 0.2)
    assert abs(err) < 1e-6, f"drift not corrected to the offset: {err:+.3f}"
    assert any("corrected -1.30s" in s or "corrected +1.30s" in s for s in statuses(q))
    print("auto mode: 1.3 s of drift corrected onto stream + offset")


def test_weak_resync_not_applied():
    ctl, stream, player, clock, q = make()
    run_sync(ctl, ctl.sync, "")
    stream.audible = False                       # every look comes back weak
    run_sync(ctl, ctl.resync, "")
    assert len(player.seeks) == 1, "a weak Resync moved the film"
    assert any("NOT applied" in s for s in statuses(q))

    ctl, stream, player, clock, q = make()
    stream.audible = False
    run_sync(ctl, ctl.sync, "")
    assert len(player.seeks) == 1, "first sync should still use its best guess"
    print("weak match: Resync leaves the film alone; first sync still guesses")


def test_slow_film_start_says_why():
    """A film slow to start on a sync (macOS asking about its folder, a
    drive spinning up) gets a status saying what it is waiting for, and
    the sync still lands."""
    ctl, stream, player, clock, q = make()
    player.slow_start = True
    run_sync(ctl, ctl.sync, "")
    msgs = statuses(q)
    assert any(s.startswith("Opening the film...") for s in msgs), msgs
    assert abs(player.pos() - stream.pos()) < 1e-6, "the slow start lost the sync"
    print("slow film start: says why it waits, and still lands")


def test_weak_first_look_listens_longer():
    ctl, stream, player, clock, q = make()
    stream.weak_until = 10.0                     # 6 s is not enough, 12 s is
    run_sync(ctl, ctl.sync, "")
    msgs = statuses(q)
    assert any("listening longer (12 s)" in s for s in msgs), msgs
    assert abs(player.pos() - stream.pos()) < 1e-6
    print("weak first look: extended to 12 s and matched")


def test_auto_passes_timed_from_their_start():
    # "checking every 30 s" means every 30 s: timing each pass from its end
    # added the listen to every interval
    ctl, stream, player, clock, q = make()
    ctl.auto_enabled = True
    run_sync(ctl, ctl.sync, "")
    passes = []
    hear = controller.audio_matcher.find_match_audio

    def find(path, feats, lo=None, hi=None, progress=None):
        t0, seconds = feats
        if seconds == controller.AUTO_RECORD_SECONDS:
            passes.append(t0)             # a pass's first look starts it
        return hear(path, feats, lo, hi, progress)
    controller.audio_matcher.find_match_audio = find
    start = clock.now
    run_loop(ctl, clock, 200)
    ts = [t - start for t in passes]
    gaps = [round(y - x, 1) for x, y in zip(ts, ts[1:])]
    assert gaps and all(g <= ctl.auto_interval + 0.5 for g in gaps), \
        f"checking every {ctl.auto_interval} s took {gaps}"
    print(f"auto cadence: checks every {max(gaps):.0f} s")

def test_vanished_film_gives_up():
    ctl, stream, player, clock, q = make()
    ctl.auto_enabled = True
    run_sync(ctl, ctl.sync, "")
    drain(q)
    controller.diagnostics = Log()      # what the failures log, not the sync
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


def test_give_up_needs_a_run():
    a = controller.matcher.MatchError("Could not decode audio from the file "
                                      "in that range.")
    b = OSError("[Errno 2] No such file or directory")

    def run(errors, arm=()):
        """`arm`: (after, on) pairs ticking Auto on or off meanwhile."""
        ctl, stream, player, clock, q = make()
        ctl.auto_enabled = True
        run_sync(ctl, ctl.sync, "")
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
    # different failures in turn are not the same failure repeating - and
    # a failure is what went wrong, not just its type: one kind with two
    # messages is two failures, and so is one message from two kinds
    other = controller.matcher.MatchError("Audio scan produced no candidates.")
    for c in (b, other, RuntimeError(str(a))):
        ctl, offs, left = run([a, c] * 4)
        assert not offs and ctl.auto_enabled and not left, (c, offs)
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
    # auto mode no longer pauses for a silent stream (the watcher does):
    # silence is a miss, tried again - never a failure that gives up
    ctl, stream, player, clock, q = make()
    ctl.auto_enabled = True
    run_sync(ctl, ctl.sync, "")
    drain(q)

    def silence():
        raise RuntimeError("Captured only silence.")
    ctl._loopback().on_capture = silence
    run_loop(ctl, clock, 120)
    got = drain(q)
    assert player.playing, "auto mode paused the film - that is the watcher's to do"
    assert not any(e[0] == "auto_off" for e in got)
    assert not any("check failed" in e[1] for e in got if e[0] == "status")
    print("silent capture: a miss, tried again, the film left playing")

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
    run_sync(ctl, ctl.sync, "")
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

class FilmPlayer(Player):
    """A player that has to be given the film first, as EmbeddedPlayer
    and ExternalPlayer do: until then a sync fails the way theirs does."""
    def __init__(self, clock):
        super().__init__(clock)
        self.films = []

    def load(self, path):
        self.films.append(path)
        self.anchor, self.playing = None, False

    def seek(self, t):
        self.anchor = (t, self.now())

    def sync_seek(self, match_t, t0, offset, waiting=None):
        if not self.films:
            raise controller.VLCError("No video file loaded.")
        super().sync_seek(match_t, t0, offset, waiting)


def switchable():
    """A controller whose players both need the film loaded, with
    external VLC standing by."""
    clock = Clock()
    stream = Stream(clock)
    install_fakes(stream)
    embedded, external = FilmPlayer(clock), FilmPlayer(clock)
    q = queue.Queue()
    ctl = controller.SyncController(q, embedded,
                                    monitor_factory=lambda name: Monitor(clock))
    ctl._now = clock
    ctl._log = lambda *a, **k: None
    ctl.external = external             # no real VLC to start
    return ctl, stream, embedded, external, q


def test_switch_back_loads_the_film():
    # the usual way: a film opened in the built-in player, synced and
    # paused, is neither reloaded nor restarted by a trip to VLC and back
    ctl, stream, embedded, external, q = switchable()
    ctl.load_file("film.mkv")
    run_sync(ctl, ctl.sync, "")
    assert embedded.seeks, statuses(q)
    embedded.pause()
    here = embedded.pos()
    run_sync(ctl, ctl.use_external, False)
    assert external.films == ["film.mkv"]
    ctl.use_embedded()
    assert embedded.films == ["film.mkv"] and embedded.pos() == here, \
        f"switching back reloaded the film opened in the built-in " \
        f"player: {embedded.films}, at {embedded.pos()} not {here}"

    ctl, stream, embedded, external, q = switchable()
    # a film opened while external VLC has playback goes to VLC alone...
    run_sync(ctl, ctl.use_external, False)
    run_sync(ctl, ctl.load_file, "film.mkv")
    assert external.films == ["film.mkv"] and embedded.films == []
    # ...and reaches the built-in player when that takes over again
    ctl.use_embedded()
    assert ctl.player is embedded
    assert embedded.films == ["film.mkv"], \
        "switching back left the built-in player without the film"
    statuses(q)
    run_sync(ctl, ctl.sync, "")
    msgs = statuses(q)
    assert embedded.seeks and abs(embedded.pos() - stream.pos()) < 1e-6, msgs

    # a film it already holds is not reloaded: it keeps its place
    embedded.pause()
    here = embedded.pos()
    run_sync(ctl, ctl.use_external, False)
    ctl.use_embedded()
    ctl.use_embedded()                  # the menu item picked again
    assert embedded.films == ["film.mkv"] and embedded.pos() == here, \
        f"switching back reloaded the film: {embedded.films}"

    # a film opened in VLC meanwhile replaces the one it had
    run_sync(ctl, ctl.use_external, False)
    run_sync(ctl, ctl.load_file, "sequel.mkv")
    ctl.use_embedded()
    assert embedded.films == ["film.mkv", "sequel.mkv"], \
        f"the built-in player kept the film it had before: {embedded.films}"
    print("player switch: the built-in player keeps its film, gets VLC's")


def synced(events):
    return [e[1] for e in events if e[0] == "synced"]


def test_synced_events():
    # a trusted manual sync: one event, before busy_off, at the stream's spot
    ctl, stream, player, clock, q = make()
    run_sync(ctl, ctl.sync, "")
    got = drain(q)
    infos = synced(got)
    assert len(infos) == 1, got
    info = infos[0]
    assert (info["trusted"], info["applied"], info["source"]) == (True, True, "sync"), info
    assert abs(info["match"] - stream.pos(info["at"])) < 1e-6, info
    kinds = [e[0] for e in got]
    assert kinds.index("synced") < kinds.index("busy_off"), kinds

    # a weak first sync is used as a first guess; a weak later one is not
    ctl, stream, player, clock, q = make()
    stream.audible = False
    run_sync(ctl, ctl.sync, "")
    info, = synced(drain(q))
    assert (info["trusted"], info["applied"]) == (False, True), info
    run_sync(ctl, ctl.resync, "")
    info, = synced(drain(q))
    assert (info["trusted"], info["applied"], info["source"]) == (False, False, "sync"), info

    # a sync that failed decided nothing: busy_off alone
    ctl, stream, embedded, external, q = switchable()
    ctl.video_path = "film.mkv"          # named, never loaded: sync_seek raises
    run_sync(ctl, ctl.sync, "")
    got = drain(q)
    assert not synced(got) and ("busy_off",) in got, got

    # auto checks: in sync (nothing moved), then drift corrected
    ctl, stream, player, clock, q = make()
    run_sync(ctl, ctl.sync, "")
    drain(q)
    state = new_state()
    clock.now += ctl.auto_step(state)
    info, = synced(drain(q))
    assert (info["trusted"], info["applied"], info["source"]) == (True, False, "auto"), info
    player.anchor = (player.pos() - 1.3, clock())
    clock.now += ctl.auto_step(state)
    info, = synced(drain(q))
    assert (info["trusted"], info["applied"], info["source"]) == (True, True, "auto"), info
    # ...and none when the drift could not be measured (the film paused
    # while the stream was being heard): nothing was checked
    ctl._loopback().on_capture = player.pause
    clock.now += ctl.auto_step(state)
    got = drain(q)
    assert not synced(got), got
    ctl._loopback().on_capture = None

    # a resume after a pause the watcher followed
    ctl, stream, player, clock, q, state = watching()
    stream.pause()
    watch(ctl, clock, 1.0, state=state)
    drain(q)
    stream.play()
    watch(ctl, clock, 0.5, state=state)
    info, = synced(drain(q))
    assert (info["trusted"], info["applied"], info["source"]) == (True, True, "resume"), info
    print("synced events: one per decided match - sync (trusted, weak, not "
          "applied), auto only with a measured drift, resume; none on failure")


class BrokenFilmPlayer(FilmPlayer):
    """A player that cannot open one particular film."""
    def load(self, path):
        if path == "broken.mkv":
            raise controller.VLCError("VLC could not open broken.mkv")
        super().load(path)


def test_failed_load_keeps_film():
    clock = Clock()
    player = BrokenFilmPlayer(clock)
    ctl = controller.SyncController(queue.Queue(), player,
                                    monitor_factory=lambda name: Monitor(clock))
    try:
        ctl.load_file("broken.mkv")
        raise AssertionError("the failed load did not raise")
    except controller.VLCError:
        pass
    assert ctl.video_path is None and ctl._embedded_path is None, \
        f"a film that never loaded is named: {ctl.video_path!r}"
    ctl.load_file("film.mkv")
    try:
        ctl.load_file("broken.mkv")
    except controller.VLCError:
        pass
    assert ctl.video_path == "film.mkv" and ctl._embedded_path == "film.mkv", \
        (ctl.video_path, ctl._embedded_path)
    print("failed load: the film that was in stays named; none is named "
          "when there was none")


class FakeFilm:
    """A film for picture timing: shots of 1-3 s, each a smooth picture
    panning a pixel a frame, made on demand from the frame number, so any
    stretch of it can be 'decoded' or 'shown'."""

    FPS = 24000 / 1001

    def __init__(self, seed=3):
        import numpy as np
        self.np = np
        rng = np.random.default_rng(seed)
        self.starts = np.cumsum(rng.integers(24, 72, 1000))     # shot starts, in frames
        self.cache = {}

    def _shot(self, k):
        np = self.np
        if k not in self.cache:
            rng = np.random.default_rng(1000 + k)
            coarse = rng.uniform(20, 235, (9, 30))
            big = np.kron(coarse, np.ones((6, 6)))
            kern = np.ones(5) / 5
            big = np.apply_along_axis(lambda r: np.convolve(r, kern, mode="same"), 1, big)
            big = np.apply_along_axis(lambda c: np.convolve(c, kern, mode="same"), 0, big)
            self.cache[k] = big[:36, :180]
        return self.cache[k]

    def frame(self, i):
        k = int(self.np.searchsorted(self.starts, i, side="right"))
        start = 0 if k == 0 else int(self.starts[k - 1])
        d = min(i - start, 110)
        return self._shot(k)[:, d:d + 64].astype(self.np.uint8)

    def at(self, t):
        """The frame showing at film time t."""
        return self.frame(int(self.np.floor(t * self.FPS + 1e-9)))

    def decode(self, path, t0, dur, size, crop=None):
        np = self.np
        first = int(np.ceil(t0 * self.FPS - 1e-9))
        last = int(np.floor((t0 + dur) * self.FPS))
        idx = np.arange(first, last)
        return idx / self.FPS, np.array([self.frame(i) for i in idx])


def picture_fakes(ctl, clock, film, shown_at):
    """Give ctl a screen to grab, on the simulated clock: each grab (7 ms
    apart) shows film.at(shown_at(t)). A grab of the whole window (rect
    (0, 0, 1600, 900)) puts it in a page, at grab pixels x 40-104, y 20-56;
    any other rect is the picture alone. The rects grabbed are kept in
    ctl.grab.rects. Returns what it restores."""
    import numpy as np
    rng = np.random.default_rng(5)
    rects = []

    def grab(rect, seconds, width):
        rects.append(tuple(rect))
        times, images = [], []
        end = clock.now + seconds
        while clock.now < end:
            clock.now += rng.uniform(0.0065, 0.0075)
            pic = film.at(shown_at(clock.now))
            if tuple(rect) == (0, 0, 1600, 900):
                page = np.full((90, 160), 40, np.uint8)
                page[20:56, 40:104] = pic
                pic = page
            times.append(clock.now)
            images.append(pic)
        return np.array(times), np.array(images)
    grab.rects = rects
    ctl.grab = grab
    saved = (controller.picture_timing.decode_frames, controller.time)
    controller.picture_timing.decode_frames = film.decode

    def sleep(dt):
        clock.now += dt
    controller.time = types.SimpleNamespace(sleep=sleep, monotonic=clock, perf_counter=clock,
                                            strftime=time.strftime)
    return saved


def restore_picture_fakes(saved):
    controller.picture_timing.decode_frames, controller.time = saved


def test_line_up_places_film():
    ctl, stream, player, clock, q = make()
    ctl.picture_lags = {"willneff - Twitch": {"lag": 0.142, "when": "x", "cuts": 9}}
    ctl.stream_key = "willneff - Twitch"
    ctl.film_lead = 0.012
    run_sync(ctl, ctl.sync, "")
    err = player.pos() - (stream.pos() + 0.012)
    assert abs(err) < 1e-6, f"on the sound, the film is not a lead ahead: {err:+.4f}"
    ctl.set_line_up("picture")
    assert abs(player.nudges[-1] + 0.142) < 1e-9, player.nudges
    err = player.pos() - (stream.pos() + 0.012 - 0.142)
    assert abs(err) < 1e-6, f"lined up with the picture, the film is off: {err:+.4f}"
    n = len(player.seeks)
    state = new_state()
    clock.now += ctl.auto_step(state)
    assert len(player.seeks) == n, "auto mode undid the picture's lag"
    player.anchor = (player.pos() + 1.3, clock())       # the film slipped
    clock.now += ctl.auto_step(state)
    err = player.pos() - (stream.pos() + 0.012 - 0.142)
    assert abs(err) < 1e-6, f"drift corrected to the sound, not the picture: {err:+.4f}"
    ctl.set_stream_key("another - Twitch")              # no lag measured there
    assert abs(player.nudges[-1] - 0.142) < 1e-9, player.nudges
    ctl.set_stream_key("willneff - Twitch")
    ctl.set_line_up("sound")
    assert abs(player.nudges[-1] - 0.142) < 1e-9, player.nudges
    assert abs(ctl.placement_offset() - 0.012) < 1e-9
    print("line-up: the sound or the stream's picture, through syncs, checks and "
          "drift corrections; a setting change moves the film")


def test_picture_settings_saved():
    import json
    import os
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "streamsync.json")
    saved_path = controller.CONFIG_PATH
    controller.CONFIG_PATH = controller.Path(path)
    try:
        ctl, *_ = make()
        ctl.line_up, ctl.stream_key, ctl.film_lead = "picture", "willneff - Twitch", 0.0123
        ctl.picture_lags = {"willneff - Twitch": {"lag": 0.142, "when": "2026-10-28 22:15",
                                                  "cuts": 10}}
        ctl.save_config({})
        back, *_ = make()
        back.load_config()
        assert (back.line_up, back.stream_key, back.film_lead, back.picture_lags) == \
            ("picture", "willneff - Twitch", 0.0123, ctl.picture_lags)
        cfg = json.loads(open(path).read())
        cfg.update(line_up="upside down", film_lead=0.9,
                   picture_lags={"a": {"lag": 9.0}, "b": "x", "c": {"lag": 0.05}})
        open(path, "w").write(json.dumps(cfg))
        odd, *_ = make()
        odd.load_config()
        assert (odd.line_up, odd.film_lead, list(odd.picture_lags)) == ("sound", None, ["c"]), \
            (odd.line_up, odd.film_lead, odd.picture_lags)
    finally:
        controller.CONFIG_PATH = saved_path
    print("picture settings: saved and read back; values out of range are not believed")


def test_measure_stream_picture():
    ctl, stream, player, clock, q = make()
    film = FakeFilm()
    lag = 0.142
    saved = picture_fakes(ctl, clock, film, lambda t: stream.pos(t) - lag)
    try:
        run_sync(ctl, ctl.sync, "")
        drain(q)
        ctl.set_line_up("picture")
        nudges = len(player.nudges)
        assert run_sync(ctl, ctl.measure_stream_picture, lambda: (0, 0, 1600, 900),
                        "willneff - Twitch")
        done = [p for kind, *p in drain(q) if kind == "picture"]
        assert len(done) == 1 and done[0][0]["ok"], done
        info = done[0][0]
        # a cut is first seen up to a grab after it showed: half a grab late
        err = (info["lag"] - (lag + 0.0035)) * 1000
        assert abs(err) < 4.0, f"lag {info['lag'] * 1000:.1f} ms, error {err:+.1f} ms"
        assert ctl.picture_lags["willneff - Twitch"]["lag"] == round(info["lag"], 4)
        assert abs(player.nudges[nudges] + info["lag"]) < 1e-3, player.nudges[nudges:]
        assert not ctl.measuring
        # the whole window only finds the video; the cuts are timed in a band
        # through it (the video is at 400-1040 x 200-560 on screen)
        rects = ctl.grab.rects
        whole = [i for i, r in enumerate(rects) if r == (0, 0, 1600, 900)]
        bands = [r for r in rects if r != (0, 0, 1600, 900)]
        assert whole == [0] and bands, rects
        assert all(r[3] < 0.35 * 360 and 290 <= r[1] <= 330 and 380 <= r[0] <= 420
                   for r in bands), bands
        # no film sound in the stream: a clear reason, nothing changed
        stream.audible = False
        run_sync(ctl, ctl.measure_stream_picture, lambda: (0, 0, 1600, 900), "other")
        done = [p for kind, *p in drain(q) if kind == "picture"]
        assert not done[0][0]["ok"] and "sound" in done[0][0]["why"], done
        assert "other" not in ctl.picture_lags
        # a window that has gone
        stream.audible = True
        run_sync(ctl, ctl.measure_stream_picture, lambda: None, "gone")
        done = [p for kind, *p in drain(q) if kind == "picture"]
        assert not done[0][0]["ok"] and "not on screen" in done[0][0]["why"], done
    finally:
        restore_picture_fakes(saved)
    print(f"stream picture: timed {info['lag'] * 1000:.0f} ms behind its sound "
          f"({info['cuts']} cuts); film moved onto it; no sound or no window says why")


def test_film_window_timed_after_sync():
    ctl, stream, player, clock, q = make()
    film = FakeFilm()
    lead = 0.0125
    clock_at = lambda t: player.pos() - (clock.now - t) if player.playing else 0.0
    saved = picture_fakes(ctl, clock, film, lambda t: clock_at(t) - lead)
    ctl.film_picture_rect = lambda: (0, 0, 640, 360)
    try:
        run_sync(ctl, ctl.sync, "")
        timed = [p for kind, *p in drain(q) if kind == "picture"]
        assert len(timed) == 1 and timed[0][0]["what"] == "film", timed
        # a frame is first seen up to a grab after it showed: half a grab late
        err = (ctl.film_lead - (lead + 0.0035)) * 1000
        assert abs(err) < 2.0, f"lead {ctl.film_lead * 1000:.1f} ms, error {err:+.1f} ms"
        assert abs(player.nudges[-1] - ctl.film_lead) < 1e-9, player.nudges
        # the film's picture is grabbed in the band the stream's is
        assert set(ctl.grab.rects) == {controller.picture_timing.band((0, 0, 640, 360))[0]}, \
            ctl.grab.rects
        ctl.film_picture_rect = lambda: None                 # the film window hidden
        before = ctl.film_lead
        run_sync(ctl, ctl.sync, "")
        assert ctl.film_lead == before and not [p for k, *p in drain(q) if k == "picture"]
        # a still scene names too few frames: looked at again, and timed then
        ctl.film_picture_rect = lambda: (0, 0, 640, 360)
        real, looks = controller.picture_timing.film_lead, []

        def still_first(*a):
            looks.append(1)
            return None if len(looks) == 1 else real(*a)
        controller.picture_timing.film_lead = still_first
        run_sync(ctl, ctl.sync, "")
        assert len(looks) == 2 and [p for k, *p in drain(q) if k == "picture"], looks
        # never clear: three looks, a line saying so, the lead kept
        controller.picture_timing.film_lead = lambda *a: None
        lead = ctl.film_lead
        run_sync(ctl, ctl.sync, "")
        assert ctl.film_lead == lead and any(
            "could not be timed in 3 tries" in line for line in controller.diagnostics.lines), \
            controller.diagnostics.lines[-3:]
        controller.picture_timing.film_lead = real
    finally:
        restore_picture_fakes(saved)
    print(f"film window: its {lead * 1000:.1f} ms picture delay timed after a sync "
          f"({ctl.film_lead * 1000:.1f} ms) and the film run that far ahead")


def test_inexact_sync_made_exact_later():
    # the matcher works in 16 ms steps: a sync its own recording could not
    # refine (a quiet scene) once left the film 21 ms off the stream
    lines = lambda: controller.diagnostics.lines
    ctl, stream, player, clock, q = make()
    looks = []
    ctl._loopback().on_capture = lambda: looks.append(1)
    run_sync(ctl, ctl.sync, "")
    assert len(looks) == 1 and not player.nudges and \
        any("refined to" in line for line in lines()), (looks, player.nudges, lines())
    # unclear for the sync and the look after: the second look makes it exact
    ctl, stream, player, clock, q = make()
    stream.coarse, stream.unclear = 0.021, 2
    run_sync(ctl, ctl.sync, "")
    err = (player.pos() - stream.pos()) * 1000
    assert abs(err) < 1e-3, f"the film was left {err:+.1f} ms off the stream"
    assert len(player.nudges) == 1 and abs(player.nudges[0] + 0.021) < 1e-9, player.nudges
    assert any("made exact" in line and "-21.0 ms" in line for line in lines()), lines()
    # lined up with the stream's picture, it goes where that puts it
    ctl, stream, player, clock, q = make()
    ctl.film_lead, ctl.stream_key, ctl.line_up = 0.012, "s", "picture"
    ctl.picture_lags = {"s": {"lag": 0.142, "when": "x", "cuts": 9}}
    stream.coarse, stream.unclear = -0.015, 1
    run_sync(ctl, ctl.sync, "")
    err = (player.pos() - (stream.pos() + 0.012 - 0.142)) * 1000
    assert abs(err) < 1e-3, f"lined up with the picture, the film is {err:+.1f} ms off"
    # never clear: three more looks, a line saying so, the film left as matched
    ctl, stream, player, clock, q = make()
    stream.coarse, stream.unclear = 0.021, 99
    looks = []
    ctl._loopback().on_capture = lambda: looks.append(1)
    run_sync(ctl, ctl.sync, "")
    assert len(looks) == 1 + controller.REFINE_TRIES and not player.nudges, \
        (looks, player.nudges)
    assert abs(player.pos() - stream.pos() - 0.021) < 1e-9
    assert any("not made exact" in line for line in lines()), lines()
    # the stream itself moved 68 ms after the match (a stand-in VLC settling
    # after a seek): followed once a second look agrees
    ctl, stream, player, clock, q = make()
    stream.unclear = 1
    looks = []

    def moved():
        looks.append(1)
        if len(looks) == 2:
            stream.anchor = (stream.anchor[0] + 0.068, stream.anchor[1])
    ctl._loopback().on_capture = moved
    run_sync(ctl, ctl.sync, "")
    assert len(looks) == 3 and len(player.nudges) == 1 and \
        abs(player.nudges[0] - 0.068) < 1e-9, (looks, player.nudges)
    assert abs(player.pos() - stream.pos()) < 1e-9
    assert any("seen twice" in line for line in lines()), lines()
    # ...but not chased while it keeps moving
    ctl, stream, player, clock, q = make()
    stream.unclear = 1
    looks = []

    def moving():
        looks.append(1)
        if len(looks) >= 2:
            stream.anchor = (stream.anchor[0] + 0.1, stream.anchor[1])
    ctl._loopback().on_capture = moving
    run_sync(ctl, ctl.sync, "")
    assert not player.nudges and any("no second look agreed" in line for line in lines()), \
        (player.nudges, lines())
    # a new sync while it listened: the old look moves nothing
    ctl, stream, player, clock, q = make()
    stream.coarse, stream.unclear = 0.021, 1
    looks = []

    def resynced():
        looks.append(1)
        if len(looks) == 2:
            ctl._gen += 1
    ctl._loopback().on_capture = resynced
    run_sync(ctl, ctl.sync, "")
    assert not player.nudges and not any("made exact" in line for line in lines()), \
        (player.nudges, lines())
    print("inexact sync: made exact on more of the stream (21 ms put right, on the sound "
          "or the picture; a stream that moved 68 ms since, once seen twice); left alone "
          "when never clear, while the stream keeps moving, or after a new sync")



# ------------------------------------------------- pause with the stream

def watch(ctl, clock, seconds, events=(), state=None):
    """Run watch_step for `seconds` of simulated time, wide searches and
    prefetches in place; `events` are (after, fn) pairs. Returns the
    watcher state, to go on from."""
    state = state if state is not None else ctl.watch_state()
    ctl._spawn = lambda fn, *a: fn(*a)
    start = clock.now
    todo = sorted(events, key=lambda e: e[0])
    while clock.now - start < seconds:
        while todo and clock.now - start >= todo[0][0]:
            todo.pop(0)[1]()
        clock.now += ctl.watch_step(state)
    for _, fn in todo:
        fn()
    return state


def lines(q):
    return [e[2] for e in drain(q) if e[0] == "watch"]


def watching(rule="balanced"):
    """A synced film with the watcher on, watched for 2 s."""
    ctl, stream, player, clock, q = make()
    ctl.watch_enabled, ctl.watch_rule = True, rule
    run_sync(ctl, ctl.sync, "")
    state = watch(ctl, clock, 2.0)
    assert state["mode"] == "playing" and player.playing
    return ctl, stream, player, clock, q, state


def in_sync(player, stream, off=0.0):
    return player.playing and abs(player.pos() - (stream.pos() + off)) < 1e-6


def test_watch_waits_for_a_sync():
    ctl, stream, player, clock, q = make()
    player.anchor, player.playing = (0.0, clock()), True     # played, never synced
    state = watch(ctl, clock, 2.0)
    stream.pause()
    watch(ctl, clock, 3.0, state=state)
    assert player.playing, "the watcher paused a film no sync had lined up"
    assert lines(q) == [controller.WATCH_LINES["needsync"]]
    stream.play()
    run_sync(ctl, ctl.sync, "")
    watch(ctl, clock, 1.0, state=state)
    assert state["mode"] == "playing" and lines(q)[-1] == "Hearing the film"
    print("watcher: waits for a sync, then hears the film")


def test_watch_pauses_and_resumes():
    ctl, stream, player, clock, q, state = watching()
    drain(q)
    paused_at = {}

    def stop():
        stream.pause()
        paused_at["t"] = clock.now
    watch(ctl, clock, 1.0, [(0.0, stop)], state=state)
    got = drain(q)
    assert not player.playing, "the film played on after the stream paused"
    assert ("swap", True, "pause with the stream: the film's sound stopped") in got
    assert [e[2] for e in got if e[0] == "watch"] == ["Stream paused"]
    took = player.anchor[1] - paused_at["t"]
    assert took <= 0.35, f"balanced paused {took:.2f} s after the stream did"
    watch(ctl, clock, 10.0, state=state)           # the streamer is away a while
    assert not player.playing
    back = {}

    def resume():
        stream.play()
        back["t"] = clock.now
    watch(ctl, clock, 1.0, [(0.0, resume)], state=state)
    got = drain(q)
    assert in_sync(player, stream), "the film did not play on from where the stream is"
    assert ("swap", False, "pause with the stream: the stream resumed") in got
    resumed = [e[1] for e in got if e[0] == "synced"]
    assert resumed and resumed[0]["source"] == "resume"
    came = player.anchor[1] - back["t"]
    assert came <= 0.45, f"the resume took {came:.2f} s: two looks 0.3 s apart should do"
    print(f"watcher: paused {took:.1f} s after the stream, played on in place "
          f"{came:.1f} s after its return")


def test_watch_leaves_a_quiet_film_alone():
    ctl, stream, player, clock, q, state = watching()
    stream.quiet = True
    stream.pause()
    watch(ctl, clock, 5.0, state=state)
    assert player.playing, "a quiet stretch of film was taken for a pause"
    assert "Film is quiet here" in lines(q)
    print("watcher: the film's own quiet decides nothing")


def test_watch_rules_under_talk_over():
    # talk-over hides the film from every look while the stream plays on:
    # each rule pauses as it says, and the false pause recovers onto the
    # stream - which played on meanwhile - once the film is heard again
    want = {"instant": 2, "fast": 7, "balanced": 9, "steady": None}
    for rule, looks in want.items():
        ctl, stream, player, clock, q, state = watching(rule)
        stream.audible, stream.talk_ncc = False, 0.2
        start = clock.now
        watch(ctl, clock, 3.0, state=state)
        if looks is None:
            assert player.playing, f"{rule} paused on looks it was only unsure of"
            continue
        assert not player.playing, f"{rule} never paused"
        took = round((player.anchor[1] - start) / controller.WATCH_TICK) + 1   # the first at 0
        assert took == looks, f"{rule} paused after {took} looks, not {looks}"
        stream.audible = True
        watch(ctl, clock, 0.5, state=state)
        assert in_sync(player, stream), f"{rule}: the false pause did not catch up"
    print("watcher rules: instant pauses after 2 looks, fast 7, balanced 9, steady "
          "never on unsure ones; a false pause catches up with the stream")


def test_watch_obeys_play_by_hand():
    ctl, stream, player, clock, q, state = watching()
    stream.pause()
    watch(ctl, clock, 1.0, state=state)
    assert not player.playing
    player.anchor, player.playing = (player.pos(), clock()), True   # Play pressed
    watch(ctl, clock, 3.0, state=state)
    assert player.playing, "the watcher paused a film the user had just played"
    assert state["mode"] == "override"
    assert controller.WATCH_LINES["override"] in lines(q)
    stream.play()
    run_sync(ctl, ctl.sync, "")                    # a sync lines it up: heard again
    watch(ctl, clock, 0.5, state=state)
    assert state["mode"] == "playing"
    stream.pause()
    watch(ctl, clock, 1.0, state=state)
    assert not player.playing, "following again once heard: it should pause"
    print("watcher: a hand on Play is obeyed until the film is heard again")


def test_watch_corrects_drift():
    ctl, stream, player, clock, q, state = watching()
    player.anchor = (player.pos() + 0.37, clock())             # 0.37 s ahead
    n = len(player.seeks)
    watch(ctl, clock, 1.0, state=state)
    assert len(player.seeks) == n + 1, "drift over 0.35 s was not corrected once"
    assert in_sync(player, stream)
    assert any("corrected -0.37 s drift" in m for m in statuses(q))
    player.anchor = (player.pos() + 0.2, clock())              # within tolerance
    n = len(player.seeks)
    watch(ctl, clock, 1.0, state=state)
    assert len(player.seeks) == n, "drift under 0.35 s was 'corrected'"
    print("watcher: 0.37 s of drift corrected, 0.2 s left alone")


def test_watch_finds_a_far_jump():
    ctl, stream, player, clock, q, state = watching()
    windows = []
    hear = controller.audio_matcher.find_match_audio

    def find(path, feats, lo=None, hi=None, progress=None):
        windows.append((lo, hi))
        return hear(path, feats, lo, hi, progress)
    controller.audio_matcher.find_match_audio = find
    stream.pause()
    watch(ctl, clock, 8.0, state=state)      # a search waits for 4 s heard since the stop
    assert not player.playing and windows, "no wide search while paused"
    pp = state["pp"]
    lo, hi = windows[0]
    assert lo == pp - controller.WATCH_FAR_BACK and hi >= pp + controller.WATCH_FAR_AHEAD
    stream.anchor = (stream.pos() + 900.0, clock())            # the streamer jumped
    stream.play()
    watch(ctl, clock, 40.0, state=state)
    assert in_sync(player, stream), "a stream 900 s away was never found"
    assert (None, None) in windows, "the whole film was never searched"
    print(f"watcher: a 900 s jump found by a whole-film search after "
          f"{len(windows)} wide searches")


def test_watch_pause_holds_against_old_sound():
    # the recordings a paused watcher takes still hold, for a few seconds,
    # the film from before the stream stopped: a wide search over them
    # "found" the stream there and played the film while the stream stayed
    # paused (seen end to end on real audio, at 1.3 s and again at 3 s)
    ctl, stream, player, clock, q, state = watching()
    stream.echo = True
    stream.pause()
    watch(ctl, clock, 1.0, state=state)
    assert not player.playing
    n = len(player.seeks)
    watch(ctl, clock, 8.0, state=state)
    assert not player.playing and len(player.seeks) == n, \
        f"played the film {len(player.seeks) - n} times on sound from before the pause"
    stream.play()
    watch(ctl, clock, 1.0, state=state)
    assert in_sync(player, stream)
    print("watcher: a paused stream stays paused - sound from before the stop is not "
          "taken for its return")


def test_watch_keeps_its_pause_across_a_weak_resync():
    ctl, stream, player, clock, q, state = watching()
    stream.pause()
    watch(ctl, clock, 2.0, state=state)
    assert not player.playing
    worker = press(ctl, ctl.resync, "")            # claimed now; the watcher ticks meanwhile
    watch(ctl, clock, 1.0, state=state)
    assert state["mode"] == "paused", "the watcher dropped its pause while a sync ran"
    worker()                                       # the stream is paused: weak, not applied
    assert any("NOT applied" in m for m in statuses(q))
    watch(ctl, clock, 2.0, state=state)
    assert not player.playing and state["mode"] == "paused"
    stream.play()
    watch(ctl, clock, 0.5, state=state)
    assert in_sync(player, stream), "the pause was not the watcher's to lift any more"
    print("watcher: its pause survives a weak Resync, and lifts with the stream")


def test_watch_lets_go_for_another_film():
    ctl, stream, player, clock, q, state = watching()
    stream.pause()
    watch(ctl, clock, 2.0, state=state)
    ctl.load_file("other.mkv")
    ctl.video_path = "other.mkv"
    stream.play()
    watch(ctl, clock, 3.0, state=state)
    assert not player.playing and state["mode"] == "idle", \
        "the watcher 'resumed' a film it never paused"
    print("watcher: lets its pause go when another film is opened")


def test_watch_stands_aside():
    ctl, stream, player, clock, q, state = watching()
    ctl.session = session.ViewerSession("ws://x", "CODE", "film.mkv", player, q)
    stream.pause()
    watch(ctl, clock, 2.0, state=state)
    assert player.playing and state["mode"] == "idle"
    assert controller.WATCH_LINES["party"] in lines(q)
    ctl.session = None
    ctl.watch_enabled = False
    watch(ctl, clock, 2.0, state=state)
    assert player.playing, "the switch was off"
    assert lines(q)[-1] == ""
    print("watcher: stands aside for a watch party, and does nothing switched off")


def test_watch_settings_saved():
    import json
    import tempfile
    from pathlib import Path
    path = Path(tempfile.mkdtemp()) / "cfg.json"
    real = controller.CONFIG_PATH
    controller.CONFIG_PATH = path
    try:
        ctl, *_ = make()
        ctl.watch_enabled, ctl.watch_rule = False, "steady"
        ctl.save_config({})
        cfg = json.loads(path.read_text())
        assert cfg["pause_with_stream"] is False and cfg["pause_rule"] == "steady"
        assert "auto_follow" not in cfg
        ctl2, *_ = make()
        ctl2.load_config()
        assert ctl2.watch_enabled is False and ctl2.watch_rule == "steady"
        for old, on in (({"auto_follow": False}, False), ({"auto_follow": True}, True),
                        ({}, True), ({"pause_rule": "bogus"}, True)):
            path.write_text(json.dumps(old))
            ctl3, *_ = make()
            ctl3.load_config()
            assert ctl3.watch_enabled is on and ctl3.watch_rule == "balanced", old
    finally:
        controller.CONFIG_PATH = real
    print("watcher settings: saved and read back; Follow off carries over as off")


def test_watch_failures_reported():
    ctl, stream, player, clock, q, state = watching()
    controller.diagnostics = Log()

    def gone(*a, **k):
        raise controller.matcher.MatchError("Could not decode audio from the file.")
    controller.audio_matcher.film_presence = gone
    for _ in range(5):
        try:
            clock.now += ctl.watch_step(state)
        except Exception as e:
            clock.now += ctl._watch_failed(state, e)
    assert controller.WATCH_LINES["error"] in lines(q)
    assert len(controller.diagnostics.blocks) == 1, "want one traceback per run"
    assert player.playing, "an unreadable film was taken for a pause"
    print("watcher: an unreadable film is reported once and said on its line")

def main():
    test_nudged_film_left_alone()
    test_drift_corrected()
    test_weak_resync_not_applied()
    test_weak_first_look_listens_longer()
    test_slow_film_start_says_why()
    test_auto_passes_timed_from_their_start()
    test_vanished_film_gives_up()
    test_give_up_needs_a_run()
    test_silent_capture_is_a_miss()
    test_viewer_nudge_goes_to_session()
    test_session_start_mid_listen_wins()
    test_session_teardown_does_not_block()
    test_fmt_time_carries()
    test_switch_back_loads_the_film()
    test_synced_events()
    test_failed_load_keeps_film()
    test_line_up_places_film()
    test_picture_settings_saved()
    test_measure_stream_picture()
    test_film_window_timed_after_sync()
    test_inexact_sync_made_exact_later()
    test_watch_waits_for_a_sync()
    test_watch_pauses_and_resumes()
    test_watch_leaves_a_quiet_film_alone()
    test_watch_rules_under_talk_over()
    test_watch_obeys_play_by_hand()
    test_watch_corrects_drift()
    test_watch_finds_a_far_jump()
    test_watch_pause_holds_against_old_sound()
    test_watch_keeps_its_pause_across_a_weak_resync()
    test_watch_lets_go_for_another_film()
    test_watch_stands_aside()
    test_watch_settings_saved()
    test_watch_failures_reported()
    print("CONTROLLER TEST PASSED")


if __name__ == "__main__":
    main()
