"""The macOS shell's facecam swap and film window, driven with fakes.

No Mac, no window, no osascript: mac_app's window helpers are only
touched at call time, so the real MacApp is built here over a fake Tk
(windows that remember whether they are shown or fullscreen, an after()
that never fires - the test pumps the event queue itself), a fake
player, a fake controller and a fake macwindowctl that records which
thread each System Events round trip ran on. What is tested:

- fullscreen is left and given back through the film window itself;
  libvlc's own fullscreen does nothing once the film renders into our
  window, so the swap used to leave the film fullscreen over the
  browser and the next Cmd-Shift-F took two presses;
- a swap that fails hands back the fullscreen it took away and settles
  on what it left on screen: a pause after it raises the browser when
  the film is in front - a pause after a failed resume used to be
  skipped, leaving the film fullscreen over the stream - and the resume
  every sync sends does not run a failed pause (and its error) again;
- the osascript round trips never run on the Tk thread (each can block
  for seconds - the first waits on the Automation prompt - and pausing
  used to freeze the UI for that long), and the browser is looked up
  once, not on every swap;
- a rapid pause/resume/pause only plays out the state it settled on;
- the fullscreen debt survives swaps that overlap, however their
  results interleave with new pauses;
- closing stops the swap worker before the controller shuts down;
- a video-capture sync takes the film window off screen too, so the
  matcher cannot find our own picture inside the capture region;
- switching back to the built-in player shows its film window and hands
  libvlc the view, as opening a film does.
"""

import threading
import time
import types

import controller
import mac_app

FILM = "/films/film.mkv"
TK = threading.get_ident()     # the test thread plays the Tk thread


# ------------------------------------------------------------------ fake Tk

class Widget:
    """Any ttk widget or menu: remembers its options, accepts every call."""
    def __init__(self, *a, **kw):
        self.opts = dict(kw)

    def config(self, **kw):
        self.opts.update(kw)

    configure = config

    def __getattr__(self, name):
        return lambda *a, **kw: None


class Window(Widget):
    """A Toplevel or Frame: shown/withdrawn and fullscreen, as Tk has it."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.shown = True          # Tk maps a new Toplevel at once
        self.fullscreen = False

    def withdraw(self):
        self.shown = False

    def deiconify(self):
        self.shown = True

    def state(self):
        return "normal" if self.shown else "withdrawn"

    def attributes(self, *args):
        if len(args) == 2 and args[0] == "-fullscreen":
            self.fullscreen = bool(args[1])

    def winfo_id(self):
        return 0x5EED


class Root(Window):
    def __init__(self):
        super().__init__()
        self.destroyed = False

    def after(self, ms, fn=None, *args):
        pass                       # the test pumps _poll_queue itself

    def destroy(self):
        self.destroyed = True


class Var:
    def __init__(self, master=None, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class StringVar(Var):
    def __init__(self, master=None, value=""):
        super().__init__(master, value)


class Widgets:
    def __getattr__(self, name):
        return Widget


class Dialogs:
    def __init__(self):
        self.path = FILM
        self.shown = []

    def askopenfilename(self, **kw):
        return self.path

    def __getattr__(self, name):
        return lambda *a, **kw: self.shown.append((name, a))


# ------------------------------------------------------ fake engine and Mac

class Player:
    """EmbeddedPlayer as the shell sees it."""
    def __init__(self, hwnd=None):
        self.embedded = False
        self.attached = []
        self.vlc_fullscreen = []   # libvlc's own: a no-op once attached
        self.playing = False

    def attach_tk(self, win_id):
        self.attached.append(win_id)
        self.embedded = True
        return True

    def set_fullscreen(self, flag):
        self.vlc_fullscreen.append(flag)

    def is_playing(self):
        return self.playing

    def toggle_pause(self):
        self.playing = not self.playing

    def __getattr__(self, name):
        return lambda *a, **kw: None


class External:
    def __getattr__(self, name):
        return lambda *a, **kw: None


class Ctl:
    """SyncController as the shell sees it. close() notes whether the
    shell's swap `worker` had already been told to stop: given `wait`
    seconds, a stopped worker is gone, one never told is still there."""
    def __init__(self, q, player):
        self.q = q
        self.embedded = self.player = player
        self.external = None
        self.video_path = None
        self.busy = False
        self.region = None
        self.facecam_rect = None
        self.audio_device = ""
        self.auto_interval = 30
        self.auto_follow = True
        self.relay_url = "ws://localhost:8765"
        self.closed = False
        self.worker = None
        self.wait = 1.0
        self.worker_alive_at_close = None

    def load_config(self):
        return {}

    def film_to_restore(self, cfg):
        return None

    def load_file(self, path):
        self.video_path = path

    def use_external(self, mute):
        self.external = self.external or External()
        self.player = self.external

    def use_embedded(self):
        self.player = self.embedded

    def session_running(self):
        return False

    def close(self):
        if self.worker is not None:
            self.worker.join(self.wait)
            self.worker_alive_at_close = self.worker.is_alive()
        self.closed = True

    def __getattr__(self, name):
        return lambda *a, **kw: None


class Osascript:
    """macwindowctl, recording each round trip and the thread it ran on.

    Calls take `delay` seconds, like System Events does; `hold` (an Event)
    keeps activate_app waiting until the test releases it - bounded, so a
    shell that waits for it on the Tk thread fails instead of hanging.
    """
    def __init__(self, apps=("Finder", "Safari")):
        self.apps = list(apps)
        self.calls = []            # (name, arg, thread ident)
        self.delay = 0.0
        self.fail = set()
        self.hold = None
        self.started = threading.Event()

    def _run(self, name, arg=None):
        self.calls.append((name, arg, threading.get_ident()))
        if name == "activate_app":
            self.started.set()
            if self.hold is not None:
                self.hold.wait(1.0)
        if self.delay:
            time.sleep(self.delay)
        if name in self.fail:
            raise RuntimeError("Not authorized to send Apple events")

    def list_gui_apps(self):
        self._run("list_gui_apps")
        return list(self.apps)

    def activate_app(self, name):
        self._run("activate_app", name)

    def hide_app(self, name):
        self._run("hide_app", name)

    def activate_self(self):
        self._run("activate_self")

    def names(self):
        return [c[0] for c in self.calls]


def make(film=True, **osa):
    """A MacApp over the fakes, with the film loaded the way Cmd-O does."""
    fake_tk = types.SimpleNamespace(
        Toplevel=Window, Frame=Window, Menu=Widget, Text=Widget,
        StringVar=StringVar, BooleanVar=Var, IntVar=Var)
    mac = Osascript(**osa)
    mac_app.tk = fake_tk
    mac_app.ttk = Widgets()
    mac_app.filedialog = Dialogs()
    mac_app.messagebox = Dialogs()
    mac_app.EmbeddedPlayer = Player
    mac_app.controller = types.SimpleNamespace(
        SyncController=Ctl, build_mask=controller.build_mask,
        zone_in_region=controller.zone_in_region)
    mac_app.macwindowctl = mac
    mac_app.audio_capture = types.SimpleNamespace(
        list_speakers=lambda: [], list_microphones=lambda: [])
    mac_app.diagnostics = types.SimpleNamespace(
        log=lambda *a, **kw: None, LOG_FILE="")
    app = mac_app.MacApp(Root())
    settle = app._swap_done
    app.results = []                    # each swap result the Tk side settled

    def counted(*result):
        app.results.append(result)
        settle(*result)
    app._swap_done = counted
    if film:
        app._choose_file()
    return app, mac


def pump(app, until, timeout=3.0):
    """Run the Tk side - _poll_queue - until `until()` holds."""
    deadline = time.monotonic() + timeout
    while True:
        app._poll_queue()
        if until():
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)


def settled(app, n):
    """Pump until the Tk side has settled the worker's n-th swap result -
    not just shown its status line, which the worker sends first."""
    return pump(app, lambda: len(app.results) >= n)


def status(app):
    return app.status_lbl.opts.get("text", "")


def fullscreen(app):
    """Film fullscreen, per the app and per the window - they must agree."""
    return app.fullscreen, app.video_win.fullscreen


# -------------------------------------------------------------------- tests

def test_fullscreen_goes_through_the_film_window():
    app, mac = make()
    assert app.video_win.shown and app.player_backend.embedded
    app._toggle_fullscreen()
    assert fullscreen(app) == (True, True)

    app._stream_swap(True)                              # the stream paused
    assert pump(app, lambda: app._swapped), mac.names()
    assert fullscreen(app) == (False, False), \
        f"the film stayed fullscreen over the browser: {fullscreen(app)}"

    app._stream_swap(False)                             # ...and resumed
    assert pump(app, lambda: not app._swapped), mac.names()
    assert fullscreen(app) == (True, True), fullscreen(app)
    assert not app._was_fullscreen
    assert app.player_backend.vlc_fullscreen == [], \
        "libvlc's fullscreen does nothing to a film in our own window"

    app._toggle_fullscreen()                            # one press, not two
    assert fullscreen(app) == (False, False)
    print("fullscreen: left and given back through the film window")


def test_failed_swap_gives_fullscreen_back():
    # the browser never comes up: Automation permission refused
    app, mac = make()
    app._toggle_fullscreen()
    mac.fail = {"activate_app"}
    app._stream_swap(True)
    assert pump(app, lambda: "App swap failed" in status(app)
                and app.fullscreen), (status(app), fullscreen(app))
    assert fullscreen(app) == (True, True) and not app._was_fullscreen
    # ...and the failure does not latch: the stream's resume finds no
    # browser to hide, and its next pause tries again
    mac.fail = set()
    app._stream_swap(False)
    idle(app)
    assert mac.names() == ["list_gui_apps", "activate_app"], \
        f"a resume after a failed pause hid the browser: {mac.names()}"
    app._stream_swap(True)
    assert pump(app, lambda: app._swapped), mac.names()
    assert mac.names().count("activate_app") == 2

    # a pause straight after a failed one, with no resume between (auto
    # follow restarted while the stream stays paused), tries again too
    app, mac = make()
    app._toggle_fullscreen()
    mac.fail = {"activate_app"}
    app._stream_swap(True)
    assert pump(app, lambda: "App swap failed" in status(app)
                and app.fullscreen), (status(app), fullscreen(app))
    mac.fail = set()
    app._stream_swap(True)
    assert pump(app, lambda: app._swapped), \
        f"the failed pause latched - the next one never ran: {mac.names()}"
    assert fullscreen(app) == (False, False) and app._was_fullscreen

    # the browser came up, but hiding it again on resume fails
    app, mac = make()
    app._toggle_fullscreen()
    app._stream_swap(True)
    assert pump(app, lambda: app._swapped)
    assert fullscreen(app) == (False, False)
    mac.fail = {"hide_app"}
    app._stream_swap(False)
    assert pump(app, lambda: "App swap failed" in status(app)
                and app.fullscreen), (status(app), fullscreen(app))
    assert fullscreen(app) == (True, True) and not app._was_fullscreen
    # ...and the stream's next pause is not skipped as a repeat of the
    # one before: the film is fullscreen again, in front of the browser
    # it has to make way for
    mac.fail = set()
    app._stream_swap(True)
    assert pump(app, lambda: mac.names().count("activate_app") == 2), \
        f"the pause after a failed resume never ran: {mac.names()}"
    assert fullscreen(app) == (False, False) and app._was_fullscreen, \
        f"the film stayed fullscreen over the stream: {fullscreen(app)}"

    # a hide that fails under a windowed film leaves the browser in front
    # of it, so the next resume tries the hide again
    app, mac = make()
    app._stream_swap(True)
    assert pump(app, lambda: app._swapped)
    mac.fail = {"hide_app"}
    app._stream_swap(False)
    assert settled(app, 2) and "App swap failed" in status(app), status(app)
    mac.fail = set()
    app._stream_swap(False)
    assert pump(app, lambda: mac.names().count("hide_app") == 2), \
        f"the resume after a failed one never ran: {mac.names()}"
    assert settled(app, 3) and not app._swapped
    # ...unless the film went fullscreen over it during the pause: then
    # the film is in front, and it is the next pause that has to run
    app, mac = make()
    app._stream_swap(True)
    assert pump(app, lambda: app._swapped)
    app._toggle_fullscreen()            # Shift-Cmd-F while paused
    mac.fail = {"hide_app"}
    app._stream_swap(False)
    assert settled(app, 2) and "App swap failed" in status(app), status(app)
    mac.fail = set()
    app._stream_swap(True)
    assert pump(app, lambda: mac.names().count("activate_app") == 2), \
        f"a film made fullscreen during the pause stayed over the stream " \
        f"once its resume failed: {mac.names()}"
    assert fullscreen(app) == (False, False) and app._was_fullscreen

    # the browser hid, but bringing the film forward failed: nothing is
    # left up, so a resume has nothing to do and the next pause raises
    # the browser again - fullscreen or not
    for full in (False, True):
        app, mac = make()
        if full:
            app._toggle_fullscreen()
        app._stream_swap(True)
        assert pump(app, lambda: app._swapped)
        mac.fail = {"activate_self"}
        app._stream_swap(False)
        assert settled(app, 2) and "App swap failed" in status(app), \
            status(app)
        assert fullscreen(app) == (full, full) and not app._was_fullscreen
        mac.fail = set()
        app._stream_swap(False)
        idle(app)
        assert mac.names().count("hide_app") == 1, \
            f"a resume after a half-done one hid the browser: {mac.names()}"
        app._stream_swap(True)
        assert pump(app, lambda: mac.names().count("activate_app") == 2), \
            f"the pause after a half-done resume never ran: {mac.names()}"

    # no browser running at all
    app, mac = make(apps=("Finder",))
    app._toggle_fullscreen()
    app._stream_swap(True)
    assert pump(app, lambda: "Pick the stream's browser" in status(app)
                and app.fullscreen), (status(app), fullscreen(app))
    assert fullscreen(app) == (True, True) and not app._was_fullscreen
    print("failed swap: fullscreen given back, what is on screen settles it")


def idle(app, seconds=0.3):
    """Keep the Tk side running a while, for things that must NOT happen."""
    pump(app, lambda: False, timeout=seconds)


def wait_for(cond, timeout=3.0):
    """Wait on the worker alone - the Tk side is busy elsewhere."""
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


MATCHED = "Matched stream audio at 9:54.0 (score 9.1, peak z 12)."
REFUSED = {"list_gui_apps", "activate_app", "hide_app", "activate_self"}


def sync_applied(app):
    """What an applied sync sends the Tk side: the resume swap, then the
    sync's result line."""
    app.q.put(("swap", False))
    app.q.put(("status", MATCHED))


def test_failed_swap_is_not_rerun_by_every_sync():
    # Automation permission refused: every System Events call fails, and
    # the pause that found out has said so. The resume each applied sync
    # sends has no browser to hide, and must not run the failing swap
    # again only to write its error over the sync's result - whether the
    # browser was picked or looked up, the film fullscreen or windowed
    for pick, full in (("Safari", False), ("", True)):
        app, mac = make()
        app.stream_app = pick
        if full:
            app._toggle_fullscreen()
        mac.fail = set(REFUSED)
        app._stream_swap(True)
        assert settled(app, 1) and fullscreen(app) == (full, full)
        tried = mac.names()
        for _ in range(3):
            sync_applied(app)
            idle(app, 0.1)
            assert status(app) == MATCHED, \
                f"the failed pause was run again over a sync: {status(app)}"
        idle(app)
        assert mac.names() == tried, \
            f"every sync ran the failed pause's swap again: {mac.names()}"

    # permission withdrawn during a pause: the resume fails and hands
    # fullscreen back, and the film is in front again - the syncs after it
    # have nothing to hide either
    app, mac = make()
    app._toggle_fullscreen()
    app._stream_swap(True)
    assert pump(app, lambda: app._swapped)
    mac.fail = set(REFUSED)
    sync_applied(app)
    assert settled(app, 2) and fullscreen(app) == (True, True)
    assert "App swap failed" in status(app), status(app)
    tried = mac.names()
    for _ in range(3):
        sync_applied(app)
        idle(app, 0.1)
        assert status(app) == MATCHED, \
            f"the failed resume was run again over a sync: {status(app)}"
    idle(app)
    assert mac.names() == tried, \
        f"every sync ran the failed resume's swap again: {mac.names()}"
    print("failed swap: not run again by every sync, whose result stays up")


def test_refused_swap_leaves_things_as_they_were():
    # The first pause sits on the Automation prompt, and the stream
    # resumes - or a sync lands - before the user answers Don't Allow.
    # Nothing was raised, so the resume's refused hide left nothing up
    # either, and the syncs after it have nothing to hide
    app, mac = make()
    app.stream_app = "Safari"
    mac.fail = set(REFUSED)
    mac.hold = threading.Event()        # the prompt is up
    app._stream_swap(True)
    assert mac.started.wait(2.0)
    app._stream_swap(False)             # resumed before it is answered
    mac.hold.set()                      # Don't Allow
    assert settled(app, 2), app.results
    tried = mac.names()
    for _ in range(3):
        sync_applied(app)
        idle(app, 0.1)
        assert status(app) == MATCHED, \
            f"a pause refused after the resume was sent left every sync " \
            f"rerunning the hide: {status(app)}"
    idle(app)
    assert mac.names() == tried, mac.names()

    # A browser we raised quits during the pause: the resume's hide fails,
    # the next sync finds no browser at all, and that settles on nothing
    # being up - so a browser opened later for something else is neither
    # looked for by every sync nor hidden by one
    app, mac = make()
    app._stream_swap(True)
    assert pump(app, lambda: app._swapped), mac.names()
    mac.apps = ["Finder"]               # Safari quit during the pause
    sync_applied(app)
    assert settled(app, 2) and "App swap failed" in status(app), status(app)
    sync_applied(app)                   # no browser to find
    idle(app)
    tried = mac.names()
    for _ in range(3):
        sync_applied(app)
        idle(app, 0.1)
    mac.apps = ["Finder", "Google Chrome"]
    sync_applied(app)
    idle(app)
    assert mac.names() == tried, \
        f"a sync went looking for a browser it never raised: " \
        f"{mac.names()[len(tried):]}"
    print("failed swap: a refused call leaves the stream app where it was")


def test_swap_runs_off_the_tk_thread():
    app, mac = make()
    mac.delay = 0.3                     # System Events on a good day
    t = time.perf_counter()
    app._stream_swap(True)
    froze = time.perf_counter() - t
    assert froze < 0.2, f"pausing froze the UI for {froze:.2f}s"
    assert pump(app, lambda: app._swapped), mac.names()
    app._stream_swap(False)
    assert pump(app, lambda: not app._swapped), mac.names()
    app._stream_swap(True)
    assert pump(app, lambda: app._swapped), mac.names()
    on_tk = [name for name, _, thread in mac.calls if thread == TK]
    assert not on_tk, f"osascript ran on the Tk thread: {on_tk}"
    assert mac.names().count("list_gui_apps") == 1, \
        f"the browser was looked up on every swap: {mac.names()}"
    print("swap: osascript runs off the Tk thread, browser looked up once")


def test_rapid_toggles_collapse():
    app, mac = make()
    mac.hold = threading.Event()
    app._stream_swap(True)              # pause: the browser is being raised
    assert mac.started.wait(2.0)
    app._stream_swap(False)             # ...while resume, pause, resume
    app._stream_swap(True)              #    pile up behind it
    app._stream_swap(False)
    mac.hold.set()
    settled = ["list_gui_apps", "activate_app", "hide_app", "activate_self"]
    pump(app, lambda: mac.names() == settled and not app._swapped)
    idle(app)
    assert mac.names() == settled and not app._swapped, mac.names()
    print("swap: a rapid pause/resume/pause plays out only where it settled")


def test_fullscreen_debt_survives_overlapping_swaps():
    # pause, resume and pause again while the first raise is still running
    app, mac = make()
    app._toggle_fullscreen()
    mac.hold = threading.Event()
    app._stream_swap(True)
    assert fullscreen(app) == (False, False), \
        f"fullscreen must be left before the browser is raised: {fullscreen(app)}"
    assert mac.started.wait(2.0)
    app._stream_swap(False)
    app._stream_swap(True)
    mac.hold.set()
    assert pump(app, lambda: app._swapped)
    idle(app)
    assert fullscreen(app) == (False, False) and app._was_fullscreen
    app._stream_swap(False)
    assert pump(app, lambda: not app._swapped and app.fullscreen), \
        f"the fullscreen debt was forgotten: {fullscreen(app)}"
    assert fullscreen(app) == (True, True) and not app._was_fullscreen

    # the resume's result reaches the Tk side only after the next pause
    app, mac = make()
    app._toggle_fullscreen()
    app._stream_swap(True)
    assert pump(app, lambda: app._swapped)
    app._stream_swap(False)
    assert wait_for(lambda: "activate_self" in mac.names())
    app._stream_swap(True)              # handled before that "swapdone"
    assert pump(app, lambda: mac.names().count("activate_app") == 2
                and app._swapped), mac.names()
    idle(app)
    assert fullscreen(app) == (False, False), \
        f"fullscreen came back over the browser: {fullscreen(app)}"
    app._stream_swap(False)
    assert pump(app, lambda: not app._swapped and app.fullscreen), \
        f"the fullscreen debt was forgotten: {fullscreen(app)}"
    assert fullscreen(app) == (True, True) and not app._was_fullscreen
    print("fullscreen: the debt survives overlapping swaps")


def test_worker_stops_before_the_controller_closes():
    app, mac = make()
    worker = getattr(app, "_swap_thread", None)
    assert worker is not None and worker.is_alive(), "no swap worker running"
    app.ctl.worker = worker
    app._on_close()
    assert app.ctl.closed and app.root.destroyed
    assert app.ctl.worker_alive_at_close is False, \
        "the swap worker was still running when the controller closed"

    # a round trip in flight finishes, but nothing queued behind it runs
    app, mac = make()
    mac.hold = threading.Event()
    app._stream_swap(True)
    assert mac.started.wait(2.0)
    app._stream_swap(False)
    app.ctl.worker, app.ctl.wait = app._swap_thread, 0.0
    app._on_close()
    mac.hold.set()
    app._swap_thread.join(2.0)
    assert not app._swap_thread.is_alive()
    assert "hide_app" not in mac.names(), mac.names()
    print("close: the swap worker stops before the controller does")


def test_video_sync_hides_the_film_window():
    app, mac = make()
    app.method_var.set("video")
    app.ctl.region = (0, 0, 1280, 720)
    sent = []
    app.ctl.sync = lambda hint, window, method, **video: \
        sent.append(video) or True
    app._sync()
    hidden = sent[0]["hidden"]
    assert app.video_win in hidden and not app.video_win.shown, \
        "the film window stayed on screen for the capture"
    assert app.root in hidden and not app.root.shown
    app.q.put(("show", hidden))         # the controller, frames grabbed
    assert pump(app, lambda: app.video_win.shown and app.root.shown)

    # a film window the user closed is not brought back by a sync
    app.video_win.withdraw()
    sent.clear()
    app._sync()
    assert app.video_win not in sent[0]["hidden"]
    app.q.put(("show", sent[0]["hidden"]))
    assert pump(app, lambda: app.root.shown)
    assert not app.video_win.shown

    # a time it cannot read puts every hidden window straight back
    app._show_video_window()

    def unreadable(*a, **kw):
        raise ValueError("Can't read that time.")
    app.ctl.sync = unreadable
    app._sync()
    assert app.root.shown and app.video_win.shown
    print("video sync: the film window is off screen for the capture")


def switch(app, kind):
    app.player_var.set(kind)
    app._apply_player_choice()


def test_switch_to_embedded_shows_the_film_window():
    # a film opened while External VLC was in use never got a window
    app, mac = make(film=False)
    switch(app, "external")
    app._choose_file()
    assert not app.video_win.shown and not app.player_backend.attached
    switch(app, "embedded")
    assert app.video_win.shown, \
        "switching to the built-in player left its film window hidden"
    assert app.player_backend.attached == [app.video_frame.winfo_id()], \
        "the built-in player was never given the film window's view"

    # a film window closed during External playback comes back, attached once
    app, mac = make()
    app.video_win.withdraw()
    switch(app, "external")
    switch(app, "embedded")
    assert app.video_win.shown and len(app.player_backend.attached) == 1

    # no film yet: no empty window
    app, mac = make(film=False)
    switch(app, "external")
    switch(app, "embedded")
    assert not app.video_win.shown
    print("player switch: the built-in player gets its film window back")


def main():
    test_fullscreen_goes_through_the_film_window()
    test_failed_swap_gives_fullscreen_back()
    test_failed_swap_is_not_rerun_by_every_sync()
    test_refused_swap_leaves_things_as_they_were()
    test_swap_runs_off_the_tk_thread()
    test_rapid_toggles_collapse()
    test_fullscreen_debt_survives_overlapping_swaps()
    test_worker_stops_before_the_controller_closes()
    test_video_sync_hides_the_film_window()
    test_switch_to_embedded_shows_the_film_window()
    print("MAC SWAP TEST PASSED")


if __name__ == "__main__":
    main()
