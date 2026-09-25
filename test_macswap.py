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
- a swap that fails hands back the fullscreen it took away.
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
    """SyncController as the shell sees it."""
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
    # ...and the failure does not latch: the next pause tries again
    mac.fail = set()
    app._stream_swap(False)
    app._stream_swap(True)
    assert pump(app, lambda: app._swapped), mac.names()
    assert mac.names().count("activate_app") == 2

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

    # no browser running at all
    app, mac = make(apps=("Finder",))
    app._toggle_fullscreen()
    app._stream_swap(True)
    assert pump(app, lambda: "Pick the stream's browser" in status(app)
                and app.fullscreen), (status(app), fullscreen(app))
    assert fullscreen(app) == (True, True) and not app._was_fullscreen
    print("failed swap: fullscreen given back, next pause tries again")


def main():
    test_fullscreen_goes_through_the_film_window()
    test_failed_swap_gives_fullscreen_back()
    print("MAC SWAP TEST PASSED")


if __name__ == "__main__":
    main()
