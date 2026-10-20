"""The Windows adapter's stream swap, driven with fakes: the film window
comes back exactly where it was.

No Windows, no window, no Win32: windowctl is replaced by a fake before
ui/platform_win.py imports it, so the real shared shell is built here with
the Windows adapter on every OS, over test_macswap's fake Tk. The film
window is a fake that keeps a rectangle the way Windows does - where it is,
and where it restores to - and comes back from Tk's withdraw and
deiconify the way the PC showed: a window snapped to half a monitor comes
back at its size from before the snap. What is tested:

- a snapped film comes back from a pause at its snapped rectangle, round
  after round (on the PC it came back smaller, at the lower right, and
  stayed there);
- a maximized film comes back maximized, at the same place;
- a fullscreen film leaves fullscreen before its placement is read, so the
  placement is the window's own, and comes back fullscreen over a window
  put back first: leaving fullscreen later returns it to the snap;
- a snapped film that is up keeps its snap when it goes fullscreen (F,
  a double-click) and when it is shown again (Open film...): Tk's
  deiconify would un-snap it, so a window that is up is only raised;
- a film that is not fullscreen is not touched by the fullscreen step on
  the way out: not shown, not moved;
- a film window the user closed or minimized stays that way through the
  pause and after it, fullscreen or not;
- a film brought back by hand during the pause is left where it is;
- the browser is raised on the pause and minimized on the resume, as
  before.
"""

import sys
import types

import test_macswap as mac
from test_macswap import Ctl, Dialogs, FakeTk, Player, Quiet, Var, Widget, Widgets

DELL = (0, 0, 2560, 1440)              # the film's monitor
SNAPPED = (1273, 0, 1294, 1400)        # Win+Right on it
BEFORE_SNAP = (1797, 541, 974, 578)    # where the PC's film came back to
MAXIMIZED = (-8, -8, 2576, 1400)
BROWSER = 0xB0


class Win(mac.Window):
    """A Toplevel as Tk on Windows has it: a state, an OS frame, and the
    two rectangles Windows keeps for a window - where it is, and where it
    restores to. Snapped, the two differ; maximized, it is at MAXIMIZED.

    deiconify() shows the window the way Tk's SW_SHOWNOACTIVATE does, as
    test_windowctl measured it on CI's Windows: a snapped one, hidden or
    up, goes back to its rectangle from before the snap. A hidden
    maximized one may come back maximized (Tk remembers) or not;
    `tk_keeps_zoom` picks which."""
    count = 0

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        Win.count += 1
        self.hwnd = 0x1000 + Win.count
        self.tkstate = "normal"
        self.rect = self.normal = (80, 80, 974, 578)
        self.snapped = False
        self.tk_keeps_zoom = False
        self.shows = 0
        self.idle_runs = 0

    def frame(self):
        return hex(self.hwnd)

    def state(self):
        return self.tkstate

    def withdraw(self):
        super().withdraw()
        self.tkstate = "withdrawn"

    def iconify(self):
        self.shown = False
        self.tkstate = "iconic"

    def deiconify(self):
        if self.shown:
            if self.snapped:
                self.rect, self.snapped = self.normal, False
            return
        super().deiconify()
        self.shows += 1
        if self.tkstate == "zoomed" or (self.tkstate == "withdrawn"
                                        and self.rect == MAXIMIZED):
            if self.tk_keeps_zoom:
                self.tkstate = "zoomed"
                return
        if self.snapped or self.rect == MAXIMIZED:
            self.rect, self.snapped = self.normal, False
        self.tkstate = "normal"

    def update_idletasks(self):
        self.idle_runs += 1

    # what the user does
    def snap(self):
        self.rect, self.snapped = SNAPPED, True

    def maximize(self):
        self.rect, self.tkstate = MAXIMIZED, "zoomed"

    def move(self, rect):
        self.rect = self.normal = rect
        self.snapped = False


class Windowctl(types.ModuleType):
    """windowctl over the Win windows above, recording what it was asked."""

    def __init__(self):
        super().__init__("windowctl")
        self.windows = {}
        self.calls = []
        self.fs_rect = None

    def win(self, hwnd):
        return self.windows[hwnd]

    # the film window
    def placement(self, hwnd):
        w = self.win(hwnd)
        self.calls.append(("placement", w.rect))
        return {"zoomed": w.tkstate == "zoomed", "normal": w.normal,
                "rect": w.rect}

    def put_placement(self, hwnd, saved):
        w = self.win(hwnd)
        self.calls.append(("put_placement", saved["rect"]))
        w.normal, w.rect, w.snapped = saved["normal"], saved["rect"], False
        w.tkstate = "zoomed" if saved["zoomed"] else "normal"

    def borderless_fullscreen(self, hwnd):
        w = self.win(hwnd)
        self.calls.append(("fullscreen", w.rect))
        saved, w.rect = w.rect, DELL
        return saved

    def unfullscreen(self, hwnd, saved):
        self.calls.append(("unfullscreen", saved))
        self.win(hwnd).rect = saved

    def monitor_rect(self, hwnd):
        return DELL

    # the browser
    def find_stream_window(self, preferred=None, title=""):
        return BROWSER

    def restore(self, hwnd):
        self.calls.append(("restore", hwnd))

    def minimize(self, hwnd):
        self.calls.append(("minimize", hwnd))

    def is_valid(self, hwnd):
        return True

    def list_windows(self):
        return []

    def find_by_pid(self, pid):
        return None

    def foreground(self):
        return 0

    def no_activate_on_click(self, hwnd):
        return object()

    def names(self):
        return [c[0] for c in self.calls]


wc = Windowctl()
sys.modules["windowctl"] = wc         # before ui.platform_win imports it

import ui.dialogs  # noqa: E402
import ui.mainwin  # noqa: E402
import ui.platform_win as platform_win  # noqa: E402
import ui.shell as shell  # noqa: E402
import ui.update  # noqa: E402
import ui.video  # noqa: E402

MODULES = (shell, platform_win, ui.video, ui.mainwin, ui.dialogs, ui.update)


def patch(name, value):
    for module in MODULES:
        if hasattr(module, name):
            setattr(module, name, value)


def make():
    """The shared shell with the Windows adapter over the fakes, with a
    film loaded the way Open film... does, playing in its window."""
    def toplevel(*a, **kw):
        w = Win(*a, **kw)
        wc.windows[w.hwnd] = w
        return w
    patch("tk", FakeTk(Toplevel=toplevel, Frame=mac.Window, Menu=Widget,
                       Label=Widget, Canvas=Widget, Button=Widget, Entry=Widget,
                       Text=Widget, StringVar=mac.StringVar, BooleanVar=Var,
                       IntVar=Var, DoubleVar=Var, TclError=Exception))
    patch("ttk", Widgets())
    patch("widgets", Widgets())
    patch("filedialog", Dialogs())
    patch("messagebox", Dialogs())
    patch("EmbeddedPlayer", Player)
    patch("controller", Quiet(SyncController=Ctl))
    patch("depcheck", Quiet(present_window=lambda w: None))
    patch("audio_capture", Quiet(list_speakers=lambda: [], list_microphones=lambda: [],
                                 default_speaker_name=lambda: ""))
    patch("matcher", Quiet(probe=lambda p: (6750.0, 0.0)))
    patch("diagnostics", Quiet(log_report=lambda *a, **kw: "the log"))
    app = shell.create_app(mac.Root(), platform=platform_win)
    app._choose_file()
    app.embedded.playing = True
    wc.calls.clear()
    return app, app.video_win


def pause(app):
    app._toggle_pause()
    assert app._swapped


def play(app):
    app._toggle_pause()
    assert not app._swapped


def test_snapped_film_comes_back_snapped():
    app, film = make()
    film.move(BEFORE_SNAP)
    film.snap()
    # the fake shows the bug: deiconify alone loses the snap
    film.withdraw()
    film.deiconify()
    assert film.rect == BEFORE_SNAP, film.rect
    film.snap()
    for _round in range(3):
        pause(app)
        assert film.tkstate == "withdrawn"
        assert wc.names() == ["placement", "restore"], wc.calls
        assert wc.calls[-1] == ("restore", BROWSER)
        wc.calls.clear()
        play(app)
        assert wc.calls[0] == ("minimize", BROWSER), wc.calls
        assert ("put_placement", SNAPPED) in wc.calls, wc.calls
        assert film.tkstate == "normal" and film.rect == SNAPPED, \
            (film.tkstate, film.rect)
        wc.calls.clear()
    print("a snapped film comes back at its snapped rectangle, three rounds running")


def test_maximized_film_comes_back_maximized():
    for keeps in (False, True):
        app, film = make()
        film.tk_keeps_zoom = keeps
        film.move((300, 200, 974, 578))
        film.maximize()
        pause(app)
        play(app)
        assert film.tkstate == "zoomed" and film.rect == MAXIMIZED, \
            (keeps, film.tkstate, film.rect)
        assert film.normal == (300, 200, 974, 578), film.normal
    print("a maximized film comes back maximized, restoring to where it did")


def test_fullscreen_film_round_trip():
    app, film = make()
    film.move(BEFORE_SNAP)
    film.snap()
    app._set_fullscreen(True)
    assert film.rect == DELL and app.fullscreen
    wc.calls.clear()
    pause(app)
    # fullscreen left first, then the window's own placement read
    assert wc.names()[:2] == ["unfullscreen", "placement"], wc.calls
    assert wc.calls[1] == ("placement", SNAPPED), wc.calls
    assert not app.fullscreen and film.tkstate == "withdrawn"
    wc.calls.clear()
    play(app)
    # put back first, then fullscreen over it: leaving restores the snap
    assert wc.names() == ["minimize", "put_placement", "fullscreen"], wc.calls
    assert wc.calls[2] == ("fullscreen", SNAPPED), wc.calls
    assert app.fullscreen and film.rect == DELL
    app._set_fullscreen(False)
    assert film.rect == SNAPPED, film.rect
    print("a fullscreen film leaves fullscreen before its placement is read, "
          "and goes fullscreen again over its own window")


def test_snapped_film_up_keeps_its_snap():
    app, film = make()
    film.move(BEFORE_SNAP)
    film.snap()
    app._toggle_fullscreen()            # F, or a double-click
    assert app.fullscreen and wc.calls == [("fullscreen", SNAPPED)], wc.calls
    app._leave_fullscreen_key()         # Esc
    assert film.rect == SNAPPED and film.snapped, film.rect
    focused = film.focused
    app._show_video_window()            # Open film... with it up
    assert film.rect == SNAPPED and film.snapped, film.rect
    assert film.focused == focused + 1, "the film was not given the keyboard"
    print("a snapped film that is up keeps its snap through fullscreen and "
          "being shown again")


def test_windowed_film_is_not_touched_on_the_way_out():
    app, film = make()
    film.move((400, 300, 974, 578))
    shows = film.shows
    pause(app)
    assert film.shows == shows, "the fullscreen step showed the film"
    assert "unfullscreen" not in wc.names(), wc.calls
    assert wc.calls[0] == ("placement", (400, 300, 974, 578)), wc.calls
    play(app)
    assert film.rect == (400, 300, 974, 578), film.rect
    print("a film that is not fullscreen: the fullscreen step neither shows "
          "nor moves it")


def test_closed_or_minimized_film_stays_so():
    for fullscreen in (False, True):
        app, film = make()
        if fullscreen:
            app._set_fullscreen(True)
        film.withdraw()                 # the close button, or Alt+F4
        wc.calls.clear()
        pause(app)
        play(app)
        assert film.tkstate == "withdrawn", (fullscreen, film.tkstate)
        assert "placement" not in wc.names() and "put_placement" not in wc.names()
        assert not app.fullscreen, "a closed film was given fullscreen back"
        assert ("restore", BROWSER) in wc.calls and ("minimize", BROWSER) in wc.calls

    app, film = make()
    film.iconify()
    pause(app)
    assert film.tkstate == "iconic"
    play(app)
    assert film.tkstate == "iconic" and film.shows == 1, (film.tkstate, film.shows)
    print("a closed or minimized film stays closed or minimized through the "
          "pause and after it")


def test_film_brought_back_by_hand_is_left_there():
    app, film = make()
    film.move(BEFORE_SNAP)
    film.snap()
    pause(app)
    app._show_video_window()           # the main window's "Show film window"
    film.move((10, 10, 974, 578))      # ...and moved by hand
    wc.calls.clear()
    play(app)
    assert "put_placement" not in wc.names(), wc.calls
    assert film.rect == (10, 10, 974, 578), film.rect
    print("a film shown again during the pause is left where it was put")


def main():
    test_snapped_film_comes_back_snapped()
    test_maximized_film_comes_back_maximized()
    test_fullscreen_film_round_trip()
    test_snapped_film_up_keeps_its_snap()
    test_windowed_film_is_not_touched_on_the_way_out()
    test_closed_or_minimized_film_stays_so()
    test_film_brought_back_by_hand_is_left_there()
    print("WINDOWS SWAP TEST PASSED")


if __name__ == "__main__":
    main()
