"""The macOS adapter's facecam swap and film window, driven with fakes.

No Mac, no window, no osascript: ui/platform_mac.py imports on every OS
and touches its helpers only at call time, so the real shared shell is
built here with the Mac adapter (ui.shell.create_app(root,
platform=platform_mac)) over a fake Tk (windows that remember whether they
are shown or fullscreen and what is bound on them, an after() that never
fires - the test pumps the event queue itself), a fake player, a fake
controller and a fake macwindowctl that records which thread each System
Events round trip ran on. It runs on Windows and Linux too. What is tested:

- fullscreen is left and given back through the film window itself;
  libvlc's own fullscreen does nothing once the film renders into our
  window, so the swap used to leave the film fullscreen over the
  browser and the next Toggle Fullscreen took two goes;
- a swap that fails hands back the fullscreen it took away and settles
  on what it left on screen: a pause after it raises the browser when
  the film is in front - a pause after a failed resume used to be
  skipped, leaving the film fullscreen over the stream - and the resume
  every sync sends does not run a failed pause (and its error) again;
- the osascript round trips never run on the Tk thread (each can block
  for seconds - the first waits on the Automation prompt - and pausing
  used to freeze the UI for that long), and the browser is looked up
  once, not on every swap - again only once a swap with it has failed
  (it may have quit) or another Stream app is picked;
- a rapid pause/resume/pause only plays out the state it settled on;
- the fullscreen debt survives swaps that overlap, however their
  results interleave with new pauses;
- a fullscreen request made while the window is still animating - which
  macOS drops without a word - is made again until the window has taken
  it: a swap that failed fast used to leave the film in a window with
  the app believing it fullscreen;
- a pause straight after a resume raises the browser only once the film
  has really left the fullscreen it was still entering - raised before,
  the browser ends up behind the film - and the same after the user's own
  Toggle Fullscreen;
- a resume that overtakes a pause still waiting for that calls it off:
  no browser over a film that is playing again, no film dropping out of
  fullscreen for a pause that is over; and closing calls it off too;
- closing stops the swap worker before the controller shuts down;
- switching back to the built-in player shows its film window and hands
  libvlc the view, as opening a film does;
- once fullscreen has settled the film window asks for the keyboard -
  entering does not make it key on a Mac, so Space, F and Esc went
  nowhere - on a toggle and when a swap hands fullscreen back;
- leaving fullscreen hides the fullscreen bar first, before the window
  leaves and before the worker can raise the browser (the bar joins
  every Space);
- the film window takes the mouse as on Windows, with the Mac's own
  buttons (Button-2, Control-click) and sideways scrolling swallowed; the
  bar shows only once fullscreen has settled and while the window really
  is fullscreen, and a resync's hold on it stands only while the app has
  the keyboard;
- the wheel's direction follows the fingers: natural scrolling (the
  default, or the setting unreadable) turns the deltas round, and the
  setting is read once;
- Space, Left, Right, F and Esc are bound on the main window, the film
  window and the bar;
- a fullscreen the green button made is left by one Esc or one F;
- switching to the VLC app leaves fullscreen and keeps the film window up;
- with no film, F does nothing at all;
- a stream lookup that System Events refuses says so, and why, instead
  of asking for a pick;
- once closed, the queue loop dispatches and settles nothing;
- Command-Q and Settings... reach the app's own close and Settings;
- a failed attach says so at once, and the diagnostics report it writes
  (Spotlight and codesign, seconds) runs off the Tk thread.
"""

import threading
import time
import types

import ui.dialogs
import ui.mainwin
import ui.platform_mac as platform_mac
import ui.shell as shell
import ui.update
import ui.video

FILM = "/films/film.mkv"
TK = threading.get_ident()     # the test thread plays the Tk thread

# every ui module the app is made of that could reach a faked name
MODULES = (shell, platform_mac, ui.video, ui.mainwin, ui.dialogs, ui.update)


def patch(name, value):
    """Swap a collaborator in every module that uses it (each module
    looks its collaborators up as globals at call time)."""
    for module in MODULES:
        if hasattr(module, name):
            setattr(module, name, value)


# ------------------------------------------------------------------ fake Tk

class Widget:
    """Any widget or menu: remembers its options, accepts every call."""
    def __init__(self, *a, **kw):
        self.opts = dict(kw)

    def config(self, **kw):
        self.opts.update(kw)

    configure = config

    def __getattr__(self, name):
        return lambda *a, **kw: None


class Window(Widget):
    """A Toplevel or Frame: shown/withdrawn and fullscreen, as Tk has it.

    A fullscreen change takes `transition` seconds, as the macOS animation
    does, and a change asked for inside that time is dropped without a
    word, as macOS drops it. Asked straight after a dropped request to
    enter, the window says it is fullscreen - for an instant, as Tk 8.6.16
    was measured to on macOS 27.

    after() records (ms, fn, args) in `later` without running anything,
    and bind() records (sequence, fn) in `bindings`."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.shown = True          # Tk maps a new Toplevel at once
        self.fullscreen = False
        self.transition = 0.0
        self.dropped = 0           # requests lost to an animation
        self.changes = 0           # ...and requests taken
        self._moving_until = 0.0
        self._lies_until = 0.0
        self.later = []
        self.bindings = []
        self.focused = 0

    def withdraw(self):
        self.shown = False

    def deiconify(self):
        self.shown = True

    def attributes(self, *args):
        if args == ("-fullscreen",):
            return int(self.fullscreen
                       or time.monotonic() < self._lies_until)
        if len(args) == 2 and args[0] == "-fullscreen":
            want = bool(args[1])
            if want == self.fullscreen:
                return             # Tk only passes on a change
            if time.monotonic() < self._moving_until:
                self.dropped += 1
                if want:
                    self._lies_until = time.monotonic() + 0.03
                return
            self.fullscreen = want
            self.changes += 1
            self._moving_until = time.monotonic() + self.transition

    def after(self, ms, fn=None, *args):
        self.later.append((ms, fn, args))
        return f"after#{len(self.later)}"

    def after_idle(self, fn, *args):
        return self.after("idle", fn, *args)

    def bind(self, sequence=None, fn=None, add=None):
        self.bindings.append((sequence, fn))

    def focus_force(self):
        self.focused += 1

    def winfo_viewable(self):
        return int(self.shown)

    def winfo_id(self):
        return 0x5EED


class Root(Window):
    def __init__(self):
        super().__init__()
        self.destroyed = False
        self.commands = {}

    def after(self, ms, fn=None, *args):
        pass                       # the test pumps _poll_queue itself

    def createcommand(self, name, fn):
        self.commands[name] = fn

    def destroy(self):
        self.destroyed = True


class Var:
    def __init__(self, master=None, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value

    def trace_add(self, *a):       # shell code does not trace; this is a guard
        return "trace"

    def trace_remove(self, *a):
        pass


class StringVar(Var):
    def __init__(self, master=None, value=""):
        super().__init__(master, value)


class FakeTk(types.SimpleNamespace):
    """tkinter as the shell sees it. Any other class is a Widget, and a
    constant reads as tkinter's own (tk.END is "end")."""
    def __getattr__(self, name):
        if name.isupper():
            return name.lower()
        if name[:1].isupper():
            return Widget
        raise AttributeError(name)


class Widgets:
    """ui.widgets, ttk: every class is a Widget."""
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


class Quiet:
    """A module whose every function does nothing, apart from the ones
    given: diagnostics must not write the real log."""
    def __init__(self, **given):
        self.__dict__.update(given)

    def __getattr__(self, name):
        return lambda *a, **kw: None


# ------------------------------------------------------ fake engine and Mac

class Player:
    """EmbeddedPlayer as the shell sees it."""
    def __init__(self, hwnd=None):
        self.embedded = False
        self.attached = []
        self.attach_ok = True
        self.vlc_fullscreen = []   # libvlc's own: a no-op once attached
        self.playing = False

    def attach_tk(self, win_id):
        self.attached.append(win_id)
        self.embedded = self.attach_ok
        return self.attach_ok

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
    An app that is not in `apps` cannot be activated or hidden, as an app
    that has quit cannot.
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
        if name in ("activate_app", "hide_app") and arg not in self.apps:
            # System Events addresses an application process by name
            raise RuntimeError(f"Can't get application process \"{arg}\".")

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


reports = []                   # the thread each diagnostics report ran on


def make(film=True, attach=True, **osa):
    """The shared shell with the Mac adapter over the fakes, with the film
    loaded the way Open film... does."""
    mac = Osascript(**osa)
    patch("tk", FakeTk(Toplevel=Window, Frame=Window, Menu=Widget, Label=Widget,
                       Canvas=Widget, Button=Widget, Entry=Widget, Text=Widget,
                       StringVar=StringVar, BooleanVar=Var, IntVar=Var, DoubleVar=Var,
                       TclError=Exception))
    patch("ttk", Widgets())
    patch("widgets", Widgets())
    patch("filedialog", Dialogs())
    patch("messagebox", Dialogs())
    patch("EmbeddedPlayer", Player)
    patch("controller", Quiet(SyncController=Ctl))
    patch("macwindowctl", mac)
    patch("depcheck", Quiet(present_window=lambda w: None))
    patch("audio_capture", Quiet(list_speakers=lambda: [], list_microphones=lambda: [],
                                 default_speaker_name=lambda: ""))
    patch("matcher", Quiet(probe=lambda p: (6750.0, 0.0)))

    def log_report(*a, **kw):
        reports.append(threading.get_ident())
        return "the log"
    patch("diagnostics", Quiet(log_report=log_report))
    app = shell.create_app(Root(), platform=platform_mac)
    app.embedded.attach_ok = attach
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


def quiet_fullscreen(app):
    """Pump until no fullscreen request is still being checked."""
    return pump(app, lambda: app._fs_until is None)


def press(app, action, widget=None):
    """A key, as its binding hands it to the shell."""
    return app._on_key(action, types.SimpleNamespace(widget=widget or app.video_win))


# -------------------------------------------------------------------- tests

def test_fullscreen_goes_through_the_film_window():
    app, mac = make()
    assert app.video_win.shown and app.embedded.embedded
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
    assert app.embedded.vlc_fullscreen == [], \
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
    app._toggle_fullscreen()            # Toggle Fullscreen while paused
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
        app.stream_window = pick
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
    app.stream_window = "Safari"
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


def activated(mac):
    return [arg for name, arg, _ in mac.calls if name == "activate_app"]


def test_browser_is_looked_up_again():
    # the browser found and remembered quits, and another one is open
    app, mac = make()
    app._stream_swap(True)
    assert pump(app, lambda: app._swapped)
    app._stream_swap(False)
    assert pump(app, lambda: not app._swapped)
    mac.apps = ["Finder", "Google Chrome"]
    app._stream_swap(True)              # Safari has gone: this one fails
    assert settled(app, 3) and "App swap failed" in status(app), status(app)
    app._stream_swap(True)              # ...and the next one looks again
    assert pump(app, lambda: app._swapped), \
        f"a browser that quit was kept after its swap failed: {activated(mac)}"
    assert activated(mac) == ["Safari", "Safari", "Google Chrome"]
    assert mac.names().count("list_gui_apps") == 2, mac.names()

    # a Stream app picked by hand replaces the browser found automatically
    app, mac = make(apps=("Finder", "Safari", "Google Chrome"))
    app._stream_swap(True)
    assert pump(app, lambda: app._swapped)
    app._stream_swap(False)
    assert pump(app, lambda: not app._swapped)
    app.stream_var.set("Google Chrome")
    app._on_stream_pick()
    # (the pick wins over the remembered browser anyway; this is that
    # nothing found under the old choice outlives it)
    assert app._swap_app == "", \
        f"the browser found automatically outlived the pick: {app._swap_app}"
    app._stream_swap(True)
    assert pump(app, lambda: app._swapped)
    assert activated(mac) == ["Safari", "Google Chrome"], activated(mac)
    print("swap: a browser that quit, or a new pick, is looked up again")


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


def test_dropped_fullscreen_request_is_made_again():
    # the first run on a Mac, Automation refused: the swap fails while the
    # film window is still animating out of fullscreen, so the fullscreen
    # handed back is a request macOS drops
    app, mac = make()
    app._toggle_fullscreen()
    app.video_win.transition = 0.4
    mac.fail = {"activate_app"}
    app._stream_swap(True)
    assert pump(app, lambda: "App swap failed" in status(app)
                and app.fullscreen), (status(app), fullscreen(app))
    assert app.video_win.dropped, \
        "the request was not dropped, so this proves nothing"
    assert pump(app, lambda: fullscreen(app) == (True, True)), \
        f"fullscreen was handed back to a window that was still leaving " \
        f"it, and never asked for again: {fullscreen(app)}"
    assert not app._was_fullscreen
    # ...and once the window has it, it is left alone
    asked = app.video_win.dropped
    idle(app, 0.6)
    assert app.video_win.dropped == asked and app._fs_until is None

    # the same on the way out: Toggle Fullscreen twice inside the animation
    app, mac = make()
    app.video_win.transition = 0.4
    app._toggle_fullscreen()
    app._toggle_fullscreen()
    assert fullscreen(app) == (False, True) and app.video_win.dropped
    assert pump(app, lambda: fullscreen(app) == (False, False)), \
        f"the film stayed fullscreen behind the app's back: {fullscreen(app)}"

    # a window that never takes it is believed, so that the next
    # Toggle Fullscreen is one go and not two
    app, mac = make()
    settle, platform_mac.FULLSCREEN_SETTLE = platform_mac.FULLSCREEN_SETTLE, 0.5
    try:
        app.video_win.transition = 60.0
        app._toggle_fullscreen()
        app._toggle_fullscreen()            # dropped, and will stay dropped
        assert fullscreen(app) == (False, True)
        assert pump(app, lambda: app.fullscreen), \
            "the app went on believing a fullscreen change that never came"
        assert fullscreen(app) == (True, True) and app._fs_until is None
    finally:
        platform_mac.FULLSCREEN_SETTLE = settle
    print("fullscreen: a request dropped mid-animation is made again")


def watch_raises(app, mac):
    """Whether the film window was fullscreen each time the browser was
    raised, as a list that grows."""
    film_was_fullscreen = []
    raise_browser = mac.activate_app

    def activate_app(name):
        film_was_fullscreen.append(app.video_win.fullscreen)
        raise_browser(name)
    mac.activate_app = activate_app
    return film_was_fullscreen


def entering_fullscreen(transition=0.4):
    """A film paused and resumed, at the moment fullscreen is handed back:
    for `transition` seconds the window drops any request to leave."""
    app, mac = make()
    app._toggle_fullscreen()
    app._stream_swap(True)
    assert pump(app, lambda: app._swapped) and fullscreen(app) == (False, False)
    app.video_win.transition = transition
    app._stream_swap(False)
    assert pump(app, lambda: fullscreen(app) == (True, True))
    return app, mac


def parked(app):
    """An Event that is set once the swap worker is waiting for the film
    to leave fullscreen - until then a swap queued behind the pause would
    simply replace it, and nothing about the wait would be tested."""
    waiting = threading.Event()
    wait = app._left_fullscreen.wait

    def waited(timeout=None):
        waiting.set()
        return wait(timeout)
    app._left_fullscreen.wait = waited
    return waiting


def test_pause_waits_for_a_film_still_entering_fullscreen():
    # The worker's own patience is put far beyond every timeout here: a
    # worker that is never told the film has left would otherwise give up
    # and raise the browser just as these waits run out, and pass by luck.
    settle, platform_mac.FULLSCREEN_SETTLE = platform_mac.FULLSCREEN_SETTLE, 30.0
    try:
        app, mac = entering_fullscreen()
        film_was_fullscreen = watch_raises(app, mac)
        app._stream_swap(True)                          # paused at once
        assert fullscreen(app) == (False, True) and app.video_win.dropped, \
            "the film left at once, so this proves nothing"
        assert pump(app, lambda: film_was_fullscreen), \
            f"the pause was never told the film had left: {mac.names()}"
        assert film_was_fullscreen == [False], \
            "the browser was raised while the film was still fullscreen; " \
            "leaving fullscreen afterwards puts the film back in front of it"
        assert pump(app, lambda: app._swapped)
        assert fullscreen(app) == (False, False) and app._was_fullscreen

        # the same when the request macOS dropped was the user's own:
        # Toggle Fullscreen on and off inside one animation, then a pause
        app, mac = make()
        app.video_win.transition = 0.4
        film_was_fullscreen = watch_raises(app, mac)
        app._toggle_fullscreen()
        app._toggle_fullscreen()
        assert fullscreen(app) == (False, True) and app.video_win.dropped
        app._stream_swap(True)
        assert pump(app, lambda: film_was_fullscreen), mac.names()
        assert film_was_fullscreen == [False], \
            "the browser was raised under a film still leaving fullscreen"
        assert pump(app, lambda: app._swapped)
        assert fullscreen(app) == (False, False) and not app._was_fullscreen, \
            "the user had left fullscreen: there is none to give back"

        # the usual pause - the film leaves at once - does not wait on the
        # Tk side at all
        app, mac = make()
        app._toggle_fullscreen()
        app._stream_swap(True)
        assert wait_for(lambda: "activate_app" in mac.names()), \
            "a pause waited for the Tk side before raising the browser"
        assert pump(app, lambda: app._swapped)

        # ...nor for a fullscreen the app never asked for and is not
        # settling (the green button): nothing would ever end that wait
        app, mac = make()
        app.video_win.fullscreen = True
        app._stream_swap(True)
        assert wait_for(lambda: "activate_app" in mac.names()), \
            "a pause waited on a fullscreen that nothing is settling"
    finally:
        platform_mac.FULLSCREEN_SETTLE = settle
    print("fullscreen: a pause waits for a film that is still entering it")


def test_overtaken_pause_is_called_off():
    # pause, resume, and both again within a second: the second pause is
    # still waiting for the film to leave fullscreen when its resume comes
    app, mac = entering_fullscreen()
    mac.delay = 0.3                     # System Events takes its time
    waiting = parked(app)
    raised, changes = activated(mac), app.video_win.changes
    app._stream_swap(True)
    assert fullscreen(app) == (False, True) and waiting.wait(2.0), \
        "the pause is not waiting"
    app._stream_swap(False)
    # long enough for the dropped leave to have been asked for again and
    # taken, and the browser raised after it, had neither been called off
    idle(app, 1.5)
    assert activated(mac) == raised, \
        f"the browser was raised for a pause already over: {mac.names()}"
    assert app.video_win.changes == changes, \
        "the film dropped out of fullscreen for a pause already over"
    assert fullscreen(app) == (True, True) and not app._was_fullscreen
    assert not app._swapped and app._left_fullscreen.is_set()
    # ...and nothing is left hanging: the next pause runs as usual
    mac.delay = 0.0
    app.video_win.transition = 0.0
    app._stream_swap(True)
    assert pump(app, lambda: app._swapped), mac.names()
    assert fullscreen(app) == (False, False) and app._was_fullscreen

    # closing calls a waiting pause off too, and does not wait for it
    app, mac = entering_fullscreen()
    waiting = parked(app)
    raised = activated(mac)
    app._stream_swap(True)
    assert waiting.wait(2.0), "the pause is not waiting"
    app.ctl.worker = app._swap_thread
    app._on_close()
    assert app.ctl.worker_alive_at_close is False, \
        "the swap worker was still waiting when the controller closed"
    assert activated(mac) == raised, \
        f"the browser was raised as the app closed: {mac.names()}"
    print("swap: a pause overtaken while it waits is called off")


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


def switch(app, kind):
    app.player_var.set(kind)
    app._apply_player_choice()


def test_switch_to_embedded_shows_the_film_window():
    # a film opened while External VLC was in use never got a window
    app, mac = make(film=False)
    switch(app, "external")
    app._choose_file()
    assert not app.video_win.shown and not app.embedded.attached
    switch(app, "embedded")
    assert app.video_win.shown, \
        "switching to the built-in player left its film window hidden"
    assert app.embedded.attached == [app.video_frame.winfo_id()], \
        "the built-in player was never given the film window's view"

    # a film window closed during External playback comes back, attached once
    app, mac = make()
    app.video_win.withdraw()
    switch(app, "external")
    switch(app, "embedded")
    assert app.video_win.shown and len(app.embedded.attached) == 1

    # no film yet: no empty window
    app, mac = make(film=False)
    switch(app, "external")
    switch(app, "embedded")
    assert not app.video_win.shown
    print("player switch: the built-in player gets its film window back")


# --------------------------------------------------- the shared shell's film

def focus_asked(app):
    """Whether the film window has asked, for later, for the keyboard."""
    return any(fn == app.video_win.focus_force for _, fn, _ in app.video_win.later)


def test_settled_fullscreen_gives_the_film_the_keyboard():
    # entering fullscreen does not make the film's window key on a Mac:
    # Space, F and Esc over the film went nowhere
    app, mac = make()
    app.video_win.later.clear()
    app._toggle_fullscreen()
    assert quiet_fullscreen(app) and fullscreen(app) == (True, True)
    assert focus_asked(app), "a settled fullscreen left the film without the keyboard"

    # ...and when a swap hands fullscreen back
    app._stream_swap(True)
    assert pump(app, lambda: app._swapped) and quiet_fullscreen(app)
    app.video_win.later.clear()
    app._stream_swap(False)
    assert pump(app, lambda: not app._swapped and app.fullscreen and app._fs_until is None)
    assert focus_asked(app), "fullscreen handed back left the film without the keyboard"

    # ...but not on the way out
    app.video_win.later.clear()
    app._toggle_fullscreen()
    assert quiet_fullscreen(app) and fullscreen(app) == (False, False)
    assert not focus_asked(app)
    print("fullscreen: once settled, the film takes the keyboard")


def test_leaving_hides_the_bar_first():
    # the bar joins every Space: left up, it would sit over the browser
    app, mac = make()
    app._toggle_fullscreen()
    assert quiet_fullscreen(app)
    order = []
    withdraw, attributes, put = app.fs_bar.withdraw, app.video_win.attributes, app._swap_q.put

    def bar_withdraw():
        order.append("bar hidden")
        withdraw()

    def film_attributes(*args):
        if len(args) == 2 and args[0] == "-fullscreen" and not args[1]:
            order.append("film leaves")
        return attributes(*args)

    def queued(item):
        order.append("worker told")
        put(item)
    app.fs_bar.withdraw = bar_withdraw
    app.video_win.attributes = film_attributes
    app._swap_q.put = queued
    app._stream_swap(True)
    assert {"bar hidden", "film leaves", "worker told"} <= set(order), order
    assert order.index("bar hidden") < order.index("film leaves") \
        < order.index("worker told"), order
    assert not app.fs_bar.shown
    assert pump(app, lambda: app._swapped), mac.names()
    print("fullscreen: leaving hides the bar first")


FILM_MOUSE = ("<Double-Button-1>", "<MouseWheel>", "<Shift-MouseWheel>", "<Button-3>",
              "<Button-2>", "<Control-Button-1>", "<Motion>")


def bar_asked(app):
    """Whether the film window has asked, for later, to show the bar."""
    return any(fn == app._show_fs_bar for _, fn, _ in app.video_win.later)


def test_film_mouse_controls_and_bar():
    assert platform_mac.Adapter.FILM_CONTROLS is True
    app, mac = make()
    bound = dict(app.video_win.bindings)
    missing = [s for s in FILM_MOUSE if s not in bound]
    assert not missing, f"the film window lacks {missing}"
    # sideways scrolling (Shift-wheel on Aqua) is swallowed, not a volume step
    assert bound["<Shift-MouseWheel>"](types.SimpleNamespace(delta=40)) == "break"
    app._place_fs_bar = lambda: None    # the fake Tk has no geometry to place by

    # windowed: no bar
    app._show_fs_bar()
    assert not app._fs_up and not app.fs_bar.shown
    # asked for, not yet settled: no bar - it would show at the old size,
    # or over a window macOS is about to drop the request for
    app.video_win.later.clear()
    app._toggle_fullscreen()
    assert app.fullscreen and app._fs_until is not None
    app._show_fs_bar()
    assert not app._fs_up and not app.fs_bar.shown
    # settled: the film asks for the keyboard and the bar
    assert quiet_fullscreen(app) and fullscreen(app) == (True, True)
    assert focus_asked(app) and bar_asked(app)
    app._show_fs_bar()
    assert app._fs_up and app.fs_bar.shown
    # the green button takes the window out behind the app's back: the
    # bar's next tick hides it, and a move over the film brings nothing
    app.video_win.fullscreen = False
    app._fs_tick()
    assert not app._fs_up and not app.fs_bar.shown
    app._show_fs_bar()
    assert not app._fs_up and not app.fs_bar.shown
    print("film window: the mouse bound with the Mac's buttons; the bar only "
          "over a settled, real fullscreen")


def test_held_bar_stands_while_the_app_has_the_keyboard():
    # a resync from the bar holds it up; gone to another app meanwhile
    # (Command-Tab), the topmost bar must not sit over that app
    app, mac = make()
    app.root.focus_get = lambda: app.video_win
    assert app._bar_hold_stands()
    app.root.focus_get = lambda: None         # Tk: another app is active
    assert not app._bar_hold_stands()

    def unresolvable():
        raise KeyError("#menu")                # focus on a Tk-made clone
    app.root.focus_get = unresolvable
    assert app._bar_hold_stands()
    print("bar: a resync's hold stands only while the app has the keyboard")


def test_wheel_follows_the_fingers():
    runs = []

    def answer(stdout, returncode=0):
        def run(cmd, **kw):
            runs.append(cmd)
            if stdout is None:
                raise OSError("no defaults here")
            return types.SimpleNamespace(stdout=stdout, returncode=returncode)
        return types.SimpleNamespace(run=run)

    real = platform_mac.subprocess
    try:
        # natural scrolling, the key missing (never changed: natural),
        # unreadable, and natural off
        for stdout, code, sign in (("1\n", 0, -1), ("", 1, -1), (None, 0, -1), ("0\n", 0, 1)):
            app, mac = make()
            platform_mac.subprocess = answer(stdout, code)
            runs.clear()
            assert app._wheel_sign() == sign, (stdout, app._wheel_sign())
            assert app._wheel_sign() == sign and len(runs) == 1, runs   # read once
            assert runs[0][-1] == "com.apple.swipescrolldirection", runs
    finally:
        platform_mac.subprocess = real

    # through the film window: fingers up under natural scrolling sends
    # negative deltas, and turns the volume up
    app, mac = make()
    app._wheel_dir = -1
    app.video_win.later.clear()
    wheel = dict(app.video_win.bindings)["<MouseWheel>"]
    wheel(types.SimpleNamespace(delta=-3))
    assert (1, app._step_volume, (5,)) in app.video_win.later, app.video_win.later
    print("wheel: natural scrolling turns the deltas round, read once; fingers up is louder")


KEYS = ("<space>", "<Left>", "<Right>", "<KeyPress-f>", "<KeyPress-F>", "<Escape>")


def test_keys_are_bound_everywhere():
    app, mac = make()
    for name in ("root", "video_win", "fs_bar"):
        bound = {s for s, _ in getattr(app, name).bindings}
        missing = [k for k in KEYS if k not in bound]
        assert not missing, f"{name} lacks {missing}"
    # ...and a key's binding reaches the shell: Space over the film plays
    space = dict(app.video_win.bindings)["<space>"]
    assert space(types.SimpleNamespace(widget=app.video_win)) == "break"
    assert app.embedded.playing
    print("keys: bound on the main window, the film and the bar")


def test_green_button_fullscreen_is_left_in_one_press():
    # the green button (or Ctrl-Cmd-F) makes the window fullscreen behind
    # the app's back: one Esc, or one F, must leave it
    for key in ("leave", "fullscreen"):
        app, mac = make()
        app.video_win.fullscreen = True
        assert fullscreen(app) == (False, True)
        assert press(app, key) == "break"
        assert quiet_fullscreen(app)
        assert fullscreen(app) == (False, False), \
            f"{key} after the green button took two presses: {fullscreen(app)}"
    print("fullscreen: one press leaves a green-button fullscreen")


def test_switch_to_external_leaves_fullscreen():
    app, mac = make()
    app._toggle_fullscreen()
    assert quiet_fullscreen(app)
    switch(app, "external")
    assert quiet_fullscreen(app)
    assert fullscreen(app) == (False, False), fullscreen(app)
    assert app.video_win.shown, "the Mac keeps the film window up for the VLC app"
    assert not app._was_fullscreen
    print("player switch: the VLC app gets the screen, the film window stays")


def test_no_film_no_fullscreen():
    app, mac = make(film=False)
    assert press(app, "fullscreen") == "break"
    press(app, "leave")
    app._toggle_fullscreen()
    quiet_fullscreen(app)
    assert fullscreen(app) == (False, False)
    assert app.embedded.vlc_fullscreen == [] and not app.video_win.shown, \
        "fullscreen with no film covered the screen with an empty window"
    print("fullscreen: nothing to do with no film")


def test_refused_lookup_says_why():
    # no pick, and System Events refuses the lookup: asking for a pick
    # would send the user the wrong way
    app, mac = make()
    mac.fail = {"list_gui_apps"}
    app.stream_window = ""
    app._stream_swap(True)
    assert settled(app, 1)
    assert "App swap failed" in status(app) and "Automation" in status(app), status(app)
    assert not app._swapped
    print("swap: a refused lookup says why")


def test_nothing_runs_once_closed():
    app, mac = make()
    settles = []
    settle = app._settle_fullscreen

    def counted():
        settles.append(1)
        settle()
    app._settle_fullscreen = counted
    app._poll_queue()
    assert settles == [1]
    app._on_close()
    app.q.put(("status", "after the close"))
    app._poll_queue()                   # raises nothing
    assert status(app) != "after the close", "dispatched after the close"
    assert settles == [1], "settled fullscreen after the close"
    print("close: nothing is dispatched or settled afterwards")


def test_app_menu_reaches_the_app():
    # Tk's own Command-Q skips _on_close: the settings and the watch
    # party's goodbye were lost
    app, mac = make()
    commands = app.root.commands
    assert commands.get("tk::mac::Quit") == app._on_close, commands
    assert commands.get("tk::mac::ShowPreferences") == app._open_settings, commands
    commands["tk::mac::Quit"]()
    assert app.ctl.closed and app.root.destroyed and app._exit_hard
    print("app menu: Command-Q closes as the close box does")


def test_failed_attach_reports_off_the_tk_thread():
    reports.clear()
    app, mac = make(film=False, attach=False)
    app._show_video_window()            # as opening a film does first
    assert "Video embedding is unavailable" in status(app), status(app)
    assert pump(app, lambda: "Details: the log" in status(app)), status(app)
    assert reports and TK not in reports, \
        "the diagnostics report (Spotlight, codesign) ran on the Tk thread"
    print("attach failure: said at once, the report written off the Tk thread")


def main():
    test_fullscreen_goes_through_the_film_window()
    test_failed_swap_gives_fullscreen_back()
    test_failed_swap_is_not_rerun_by_every_sync()
    test_refused_swap_leaves_things_as_they_were()
    test_swap_runs_off_the_tk_thread()
    test_browser_is_looked_up_again()
    test_rapid_toggles_collapse()
    test_fullscreen_debt_survives_overlapping_swaps()
    test_dropped_fullscreen_request_is_made_again()
    test_pause_waits_for_a_film_still_entering_fullscreen()
    test_overtaken_pause_is_called_off()
    test_worker_stops_before_the_controller_closes()
    test_switch_to_embedded_shows_the_film_window()
    test_settled_fullscreen_gives_the_film_the_keyboard()
    test_leaving_hides_the_bar_first()
    test_film_mouse_controls_and_bar()
    test_held_bar_stands_while_the_app_has_the_keyboard()
    test_wheel_follows_the_fingers()
    test_keys_are_bound_everywhere()
    test_green_button_fullscreen_is_left_in_one_press()
    test_switch_to_external_leaves_fullscreen()
    test_no_film_no_fullscreen()
    test_refused_lookup_says_why()
    test_nothing_runs_once_closed()
    test_app_menu_reaches_the_app()
    test_failed_attach_reports_off_the_tk_thread()
    print("MAC SWAP TEST PASSED")


if __name__ == "__main__":
    main()
