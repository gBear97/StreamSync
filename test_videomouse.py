"""Live check of the Windows film window's mouse controls and keys.

Real Tk events on a real (small) film window: double-click toggles
fullscreen, right-click opens the player menu, the wheel steps the volume
a notch at a time without ever unmuting, and in fullscreen a moving mouse
brings up the control bar (timeline, play/pause, stop, -1/+1 frame, resync
here / full) that hides again, with the pointer, once the mouse rests,
and follows the film when it moves or resizes. The app's keys (Space,
Left/Right, F, Esc) are bound on the film window and the bar. Fullscreen
itself is checked on every monitor attached: it must cover the monitor
the window is on. Then, where VLC is installed, real libvlc plays a still
image into the window, and a click over the picture must land on our frame
- where those bindings are - not on libvlc's vout.

The wheel's arithmetic (WheelSteps) is checked first, on every OS: it is
plain counting, with Windows' and the Mac's settings.

No config: the app object is built from its film-window parts only, so
nothing here can load a film or save ~/.streamsync.json.
"""

import ctypes
import os
import queue
import sys
import tempfile
import time
import traceback
import types
from ctypes import wintypes


def wheel_steps():
    """Wheel deltas into volume steps, on any OS. Windows: 5 a notch
    (120), a precision touchpad's small deltas adding up to one, counted
    afresh when turned back. The Mac: every scroll is a stream of small
    deltas (a trackpad's run from -8 to 2), so one starting from rest
    steps at once - a mouse notch is one event - and the rest of it steps
    once per 60; with natural scrolling the deltas are turned round, so
    the fingers going up is louder either way."""
    from ui.video import WheelSteps

    # Windows: a notch is 120, and there is no rest rule
    for w in (WheelSteps(120), WheelSteps(120, sign=1)):
        got = [w.feed(d) for d in (120, 30, 30, 30, 30, 0, 40, 40, -120,
                                   120, 240, -360)]
        assert got == [1, 0, 0, 0, 1, 0, 0, 0, -1, 1, 2, -3], got
    print("wheel, Windows: a notch a step, touchpad deltas add up to one, "
          "turning back counts afresh")

    now = [100.0]

    def clock():
        return now[0]

    def feed(w, delta, dt=0.016):
        now[0] += dt
        return w.feed(delta)

    m = WheelSteps(60, sign=1, quiet=0.25, clock=clock)
    assert m.feed(0) == 0
    assert feed(m, 2, dt=5) == 1                 # from rest: at once
    steps = [feed(m, d) for d in [2, 8, 5] * 8]  # 120 more, in small deltas
    assert sorted(set(steps)) == [0, 1] and sum(steps) == 2, steps
    assert feed(m, 1, dt=0.3) == 1               # a rest again: at once
    assert feed(m, 1, dt=0.2) == 0               # not a rest: it adds up
    assert [feed(m, -30), feed(m, -30)] == [0, -1]   # turned back: afresh
    assert feed(m, -3, dt=1) == -1               # a notch down from rest
    # natural scrolling: the deltas are turned round
    n = WheelSteps(60, sign=-1, quiet=0.25, clock=clock)
    assert feed(n, 3, dt=5) == -1
    assert sum(feed(n, -6) for _ in range(10)) == 1
    print("wheel, Mac: from rest a scroll steps at once, then once per 60; "
          "natural scrolling turns the direction round")


class FakeEmbedded:
    def __init__(self):
        self.vol = None
        self.mute_calls = 0
        self.tracks = [(-1, "Disable"), (2, "English"), (3, "Francais")]
        self.current = 2
        self.picked = []
        self.t, self.n, self.playing = None, None, False
        self.seeks, self.stops = [], 0
        self.rate = 25.0             # what libvlc reports for the film

    def time(self):
        return self.t

    def length(self):
        return self.n

    def is_playing(self):
        return self.playing

    def seek(self, seconds):
        self.seeks.append(seconds)

    def stop(self):
        self.stops += 1
        self.t, self.playing = None, False

    def fps(self):
        return self.rate or None

    def volume(self):
        return self.vol

    def set_volume(self, v):
        self.vol = v

    def set_mute(self, flag):
        self.mute_calls += 1

    def subtitle_tracks(self):
        return list(self.tracks)

    def current_subtitle(self):
        return self.current

    def set_subtitle(self, tid):
        self.picked.append(tid)


def make_app(root):
    import tkinter as tk

    import windowctl
    from ui import mainwin, platform_win, shell, widgets

    # The frame-style swap is recorded, not done: the bar's checks must
    # not take the screen over for seconds at a time (monitors() checks
    # the real swap, a moment per monitor).
    # The foreground window is stubbed too: the film has the keyboard
    # unless a check hands it to another window (fg["hwnd"]), on the
    # film's monitor unless the check puts it on another (fg["monitor"]).
    fs_calls, fg = [], {}
    film_monitor = (0, 0, 1920, 1080)

    class Windowctl:
        """The stubs above; anything else is the real windowctl's."""
        borderless_fullscreen = staticmethod(
            lambda h: fs_calls.append(("on", h)) or "kept")
        unfullscreen = staticmethod(
            lambda h, saved: fs_calls.append(("off", h, saved)))
        no_activate_on_click = staticmethod(windowctl.no_activate_on_click)
        foreground = staticmethod(
            lambda: fg.get("hwnd") or int(a.video_win.frame(), 16))
        monitor_rect = staticmethod(
            lambda h: (fg.get("monitor", film_monitor)
                       if h == fg.get("hwnd") else film_monitor))

        def __getattr__(self, name):
            return getattr(windowctl, name)

    platform_win.windowctl = Windowctl()

    class FakeApp(shell.app_class(platform_win)):
        """The App with only the film window built - see the module note.
        The shell's own sync, fullscreen, key and player-switch code runs;
        the controller's side is recorded."""

        def __init__(self, root):
            self.root = root
            self._init_theme()
            self._init_platform()
            self.q = queue.Queue()
            self.fullscreen = False
            self._swapped = self._was_fullscreen = False
            self._closing = self._exit_hard = False
            self._sync_state, self._synced_at = "none", None
            self._film_length, self._pos = None, (None, None)
            self._settings = self._party = self._update_dialog = None
            self.mute_var = tk.BooleanVar(root, value=True)
            self.hint_var = tk.StringVar(root, value="1:05:00")
            self.player_var = tk.StringVar(root, value="embedded")
            self.status_lbl = tk.Label(root)
            self.sync_btn = widgets.FlatButton(root, "Sync to stream")
            self.resync_btn = widgets.FlatButton(root, "Resync")
            self.embedded = FakeEmbedded()
            self.syncs, self.nudges = [], []

            def run(kind):
                def start(hint, mute=True):
                    self.syncs.append((kind, hint))
                    self.ctl.busy = True
                    return True
                return start

            def nudge(delta):
                self.nudges.append(delta)
                return sum(self.nudges)

            def use_external(mute):
                self.ctl.player = self.ctl.external
                return None

            def use_embedded():
                self.ctl.player = self.embedded

            self.ctl = types.SimpleNamespace(
                player=self.embedded, busy=False, video_path="film.mkv",
                external=types.SimpleNamespace(fullscreen_toggle=lambda: None),
                sync=run("sync"), resync=run("resync"), nudge=nudge,
                use_external=use_external, use_embedded=use_embedded)
            self.fs_calls, self.pauses, self.popups = fs_calls, 0, []
            self.fg = fg
            self._build_video_window()
            self._build_video_menu()
            self._build_fs_bar()
            # tk_popup runs a modal menu loop that would wait for a human
            self.video_menu.tk_popup = lambda x, y: self.popups.append((x, y))

        def _toggle_pause(self):
            self.pauses += 1

        def _on_mute_toggle(self):
            pass

        def _ready(self):
            return True         # no film to choose: nothing is loaded

        def _save_config(self):
            pass

    # the main window is not built: its views have nothing to show
    for name in mainwin.MainWindow.VIEWS:
        setattr(FakeApp, name, lambda self, *a, **kw: "")

    a = FakeApp(root)
    return a


def pump(root, seconds=0.15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.01)


def click(widget, seq, **kw):
    widget.event_generate(seq, x=20, y=20, **kw)


def move(root, a, x, y):
    """The mouse moving over the film."""
    a.video_frame.event_generate("<Motion>", x=x, y=y, rootx=500 + x,
                                 rooty=300 + y)
    pump(root, 0.1)


def shown(a):
    return a._fs_up and a.fs_bar.winfo_viewable()


def mouse_controls(root, a):
    win, frame = a.video_win, a.video_frame
    win.geometry("320x180+40+40")
    win.attributes("-topmost", True)      # hit-tests need it uncovered
    win.deiconify()
    pump(root, 0.3)

    for seq in ("<Double-Button-1>", "<Button-3>", "<MouseWheel>"):
        assert win.bind(seq), f"video window has no {seq} binding"
    # no focus grab on <Enter>: it would pull typing away from the main
    # window whenever the pointer crossed the video (Space = pause)
    assert not win.bind("<Enter>")

    # double-click on the picture (the frame: clicks land there - see
    # vout_hits) toggles fullscreen, and again toggles it back, through
    # the video window's own OS frame. Tk pairs presses by their event
    # time, not the wall clock, so each press is stamped: 100 ms apart
    # within a double-click, 2 s between.
    hwnd = int(win.frame(), 16)
    t = 1000
    for want in (True, False):
        for _ in range(2):
            t += 100
            click(frame, "<ButtonPress-1>", button=1, time=t)
            click(frame, "<ButtonRelease-1>", button=1, time=t + 20)
        t += 2000
        pump(root)
        assert a.fullscreen is want, a.fs_calls
    assert a.fs_calls == [("on", hwnd), ("off", hwnd, "kept")], a.fs_calls
    print("double-click on the video: fullscreen on, then off again")

    # right-click: the menu pops at the pointer, its subtitle list rebuilt
    # with the current track selected
    click(frame, "<ButtonPress-3>", button=3, rootx=200, rooty=150)
    pump(root)
    assert a.popups == [(200, 150)], a.popups
    sub = a._sub_menu
    labels = [sub.entrycget(i, "label") for i in range(sub.index("end") + 1)
              if sub.type(i) != "separator"]
    assert labels == ["Disable", "English", "Francais",
                      "Load subtitle file..."], labels
    assert a._sub_pick.get() == 2
    sub.invoke(2)                         # "Francais"
    assert a.embedded.picked == [3], a.embedded.picked
    a.embedded.tracks = []
    a._rebuild_sub_menu()
    assert sub.entrycget(0, "state") == "disabled"
    print("right-click: menu at the pointer, subtitles listed with the "
          "current one ticked, picking one sets it")

    m = a.video_menu
    labels = [m.entrycget(i, "label") for i in range(m.index("end") + 1)
              if m.type(i) != "separator"]
    assert labels == ["Play / Pause", "Fullscreen", "Mute film sound",
                      "Volume up", "Volume down", "Subtitles",
                      "Hide video window"], labels
    assert [m.entrycget(i, "accelerator") for i in (0, 1)] == ["Space", "F"]
    m.invoke(0)
    assert a.pauses == 1
    m.invoke(3)                           # the mute checkbutton
    assert a.mute_var.get() is False
    a.mute_var.set(True)

    # a right-click that lands as the app closes must not pop anything
    a._closing = True
    a._popup_video_menu(10, 10)
    a._closing = False
    assert len(a.popups) == 1, a.popups

    # wheel: +/-5 a notch (120) from 100 when libvlc has no volume yet,
    # clamped to 0-125, and never unmutes; the OSD says so
    click(frame, "<MouseWheel>", delta=120)
    pump(root)
    assert a.embedded.vol == 105, a.embedded.vol
    assert a._osd_lbl.cget("text") == "Volume 105% (muted)"
    assert a.mute_var.get() is True and a.embedded.mute_calls == 0
    # a precision touchpad: small deltas step only once they add up to a
    # notch, and less than a notch (or a zero delta) does nothing
    for _ in range(4):
        click(frame, "<MouseWheel>", delta=30)
    pump(root)
    assert a.embedded.vol == 110, a.embedded.vol
    for d in (0, 40, 40):
        click(frame, "<MouseWheel>", delta=d)
    pump(root)
    assert a.embedded.vol == 110, a.embedded.vol
    # ...turned back, the count starts afresh: one notch down, one step
    click(frame, "<MouseWheel>", delta=-120)
    pump(root)
    assert a.embedded.vol == 105, a.embedded.vol
    a.embedded.vol = 123
    click(frame, "<MouseWheel>", delta=120)
    pump(root)
    assert a.embedded.vol == 125, a.embedded.vol
    a.embedded.vol = 3
    click(frame, "<MouseWheel>", delta=-120)
    pump(root)
    assert a.embedded.vol == 0, a.embedded.vol
    a._flash_osd("bye", ms=50)
    pump(root, 0.3)
    assert a._osd_win.state() == "withdrawn", a._osd_win.state()
    assert a._osd_win.title().startswith("StreamSync")
    print("wheel: volume steps by 5 a notch, small deltas adding up to "
          "one, clamped to 0-125, stays muted; the OSD shows it and hides "
          "itself")

    # external VLC: the video window is not its player - no menu, no
    # wheel, and a double-click neither fullscreens the embed nor reaches
    # out to VLC's own window (a blocking call from here)
    toggles = []
    external = a.ctl.external
    a.ctl.player = object()
    a.ctl.external = types.SimpleNamespace(
        fullscreen_toggle=lambda: toggles.append(1))
    click(frame, "<ButtonPress-3>", button=3, rootx=1, rooty=1)
    click(frame, "<MouseWheel>", delta=120)
    for _ in range(2):
        t += 100
        click(frame, "<ButtonPress-1>", button=1, time=t)
        click(frame, "<ButtonRelease-1>", button=1, time=t + 20)
    t += 2000
    pump(root)
    assert len(a.popups) == 1 and a.embedded.vol == 0
    assert toggles == [] and a.fullscreen is False, (toggles, a.fs_calls)
    a.ctl.player, a.ctl.external = a.embedded, external
    print("external player: right-click, wheel and double-click leave the "
          "embed (and VLC) alone")


LRESULT = ctypes.c_ssize_t
WM_MOUSEACTIVATE, WM_LBUTTONDOWN, HTCLIENT = 0x0021, 0x0201, 1
MA_ACTIVATE, MA_NOACTIVATE = 1, 3


def fs_bar(root, a):
    """The fullscreen control bar, over a window standing in for a
    fullscreen one (the frame-style swap is stubbed - see make_app)."""
    win, frame, bar, e = a.video_win, a.video_frame, a.fs_bar, a.embedded
    a.FS_BAR_IDLE = 0.4
    a._pointer_on_fs_bar = lambda: False   # wherever the real pointer is
    win.geometry("960x540+40+40")          # room for the whole bar
    pump(root, 0.3)
    e.t, e.n, e.playing = 754.4, 6750.0, True

    move(root, a, 10, 10)
    assert not a._fs_up and bar.state() == "withdrawn"
    a._set_fullscreen(True)
    move(root, a, 12, 10)
    assert shown(a)
    assert a._fs_play.cget("text") == "Pause"
    assert a._fs_time.cget("text") == "12:34", a._fs_time.cget("text")
    assert a._fs_len.cget("text") == "1:52:30", a._fs_len.cget("text")
    mid = bar.winfo_rootx() + bar.winfo_width() // 2
    want = win.winfo_rootx() + win.winfo_width() // 2
    assert abs(mid - want) <= 1, (mid, want)
    assert (bar.winfo_rooty() + bar.winfo_height()
            < win.winfo_rooty() + win.winfo_height())
    # as wide as its controls ask for at least, so the film length is not
    # squeezed out by the track (which takes what is left)
    pump(root)
    assert bar.winfo_width() >= bar.winfo_reqwidth(), \
        (bar.winfo_width(), bar.winfo_reqwidth())
    assert a._fs_len.winfo_width() >= a._fs_len.winfo_reqwidth(), \
        (a._fs_len.winfo_width(), a._fs_len.winfo_reqwidth())
    assert a._fs_track.winfo_width() > 200, a._fs_track.winfo_width()
    assert bar.title().startswith("StreamSync")
    print("fullscreen: a moving mouse brings up the bar, centred at the "
          "bottom, reading 12:34 of 1:52:30, with room for all of it")

    pump(root, 0.8)
    assert not shown(a) and bar.state() == "withdrawn"
    assert frame.cget("cursor") == "none" and win.cget("cursor") == "none"
    move(root, a, 12, 10)     # what Windows sends as the bar vanishes
    assert not shown(a) and frame.cget("cursor") == "none"
    move(root, a, 40, 30)
    assert shown(a) and frame.cget("cursor") == ""
    print("a resting mouse hides the bar and the pointer; a repeated "
          "position does not bring them back, a real move does")

    # the timeline: a click seeks there, a drag seeks once, on release
    a._fs_refresh()
    pump(root)
    tr = a._fs_track
    x0, span = tr.inset, tr.winfo_width() - 2 * tr.inset
    tr.event_generate("<ButtonPress-1>", x=x0 + span // 2, y=10)
    tr.event_generate("<ButtonRelease-1>", x=x0 + span // 2, y=10)
    pump(root)
    assert len(e.seeks) == 1 and abs(e.seeks[0] - 3375.0) < 20, e.seeks
    tr.event_generate("<ButtonPress-1>", x=x0 + span // 4, y=10)
    tr.event_generate("<Motion>", x=x0 + span * 3 // 4, y=10, state=0x100)
    pump(root)
    assert len(e.seeks) == 1, e.seeks            # not while dragging
    assert a._fs_time.cget("text").startswith("1:24:"), \
        a._fs_time.cget("text")                  # the knob's time, ~3/4
    tr.event_generate("<ButtonRelease-1>", x=x0 + span * 3 // 4, y=10)
    pump(root)
    assert len(e.seeks) == 2 and abs(e.seeks[1] - 5062.5) < 20, e.seeks
    print("timeline: a click seeks there; a drag shows its time and seeks "
          "once, on release")

    pauses = a.pauses
    a._fs_play.invoke()
    pump(root)
    assert a.pauses == pauses + 1
    a._fs_stop.invoke()
    pump(root)
    assert e.stops == 1
    assert a._fs_play.cget("text") == "Play"
    assert a._fs_time.cget("text") == "-:--"
    tr.event_generate("<ButtonPress-1>", x=x0 + 10, y=10)
    tr.event_generate("<ButtonRelease-1>", x=x0 + 10, y=10)
    pump(root)
    assert len(e.seeks) == 2, e.seeks            # stopped: nothing to seek
    print("play/pause and stop work; a stopped film has no timeline")

    # resync: the bar stays up, with its status, until the result is in;
    # either button locks both, and the one pressed says it is running
    resync, full = a._fs_resync, a._fs_full
    for btn, want in ((resync, ("resync", "1:05:00")), (full, ("sync", ""))):
        label = btn.cget("text")
        move(root, a, 50 + len(a.syncs), 30)
        assert shown(a)
        btn.invoke()
        pump(root)
        assert a.syncs[-1] == want, a.syncs
        assert resync.cget("state") == full.cget("state") == "disabled"
        assert a.sync_btn.cget("state") == "disabled"
        assert btn.cget("text") == "Resyncing..."
        a._set_status("Listening to the stream (12 s)...")
        assert a._fs_status.cget("text") == \
            "Listening to the stream (12 s)..."
        pump(root, 0.8)
        assert shown(a)
        a.ctl.busy = False
        a._fs_sync_done()
        assert resync.cget("state") == full.cget("state") == "normal"
        assert btn.cget("text") == label
        pump(root, 0.2)
        assert shown(a)
        pump(root, 0.6)
        assert not shown(a)
    assert a.syncs == [("resync", "1:05:00"), ("sync", "")], a.syncs
    print("resync here / resync (full): the bar holds while one runs, "
          "shows its status, then rests as usual")

    # Another monitor's window keeping the keyboard is the usual case (a
    # click on the bar leaves it there): the bar holds. Alt+Tab to a
    # window on the film's monitor mid-resync, though: the topmost bar
    # must not sit over it until the result is in - it rests as usual.
    # Back on the film, a move brings it up held again.
    move(root, a, 52, 30)
    resync.invoke()
    pump(root)
    assert a._fs_hold and shown(a)
    a.fg["hwnd"] = 1                  # some other application's window...
    a.fg["monitor"] = (1920, 0, 1920, 1080)      # ...on the other monitor
    pump(root, 0.8)
    assert shown(a)
    del a.fg["monitor"]               # now on the film's
    pump(root, 0.8)
    assert not shown(a)
    a.fg.clear()
    move(root, a, 53, 30)
    pump(root, 0.8)
    assert shown(a)
    a.ctl.busy = False
    a._fs_sync_done()
    pump(root, 0.8)
    assert not shown(a)
    print("a held bar holds while another monitor's window is active, and "
          "rests as usual once one on the film's monitor is")

    # a sync started elsewhere (the main window, a hotkey) locks the bar's
    # buttons too, and a click that gets to them anyway starts nothing,
    # relabels nothing and holds nothing
    syncs = len(a.syncs)
    move(root, a, 54, 30)
    a._sync()
    assert len(a.syncs) == syncs + 1 and a.ctl.busy
    assert resync.cget("state") == full.cget("state") == "disabled"
    for whole in (False, True):
        a._fs_resync_clicked(whole=whole)
    pump(root)
    assert len(a.syncs) == syncs + 1 and not a._fs_hold, a.syncs
    assert resync.cget("text") == "Resync here"
    assert full.cget("text") == "Resync (full)"
    pump(root, 0.8)
    assert not shown(a)
    a.ctl.busy = False
    a._fs_sync_done()
    assert resync.cget("state") == full.cget("state") == "normal"
    print("a sync started elsewhere locks the bar's resync buttons; one "
          "pressed then starts and holds nothing")

    # gone with whatever takes the film away
    move(root, a, 41, 30)
    win.withdraw()
    pump(root, 0.4)
    assert not shown(a)
    win.deiconify()
    move(root, a, 42, 30)
    assert shown(a)
    a._set_fullscreen(False)
    assert not shown(a) and frame.cget("cursor") == ""
    move(root, a, 43, 30)
    assert not shown(a)                          # windowed: no bar
    a._set_fullscreen(True)
    a.ctl.player = object()                      # external VLC
    move(root, a, 44, 30)
    assert not shown(a)
    a.ctl.player = e
    a._closing = True
    a._show_fs_bar()
    assert not shown(a)
    a._closing = False
    a._set_fullscreen(False)
    print("the bar goes when the film does, leaves with fullscreen, and "
          "never shows windowed, for external VLC or while closing")

    # back into fullscreen with the mouse resting (F, a double-click,
    # the film resuming after a stream pause): Windows repeats the last
    # position at most, yet the bar shows and then goes, pointer and all
    a._set_fullscreen(True)
    move(root, a, 44, 30)                        # the same place again
    assert shown(a)
    pump(root, 0.8)
    assert not shown(a) and frame.cget("cursor") == "none"
    a._set_fullscreen(False)
    print("fullscreen entered under a resting mouse: the bar shows, then "
          "goes with the pointer")


def no_activate(root, a):
    """A click on the bar must not make it the active window - the film
    keeps the keyboard (Space, Esc) and the taskbar stays under it. The
    film window, plain Tk, is the control: Tk says yes to activation."""
    send = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)(
        ("SendMessageW", ctypes.windll.user32))
    win, bar = a.video_win, a.fs_bar
    a._set_fullscreen(True)
    move(root, a, 60, 30)
    assert shown(a)
    msg = (WM_LBUTTONDOWN << 16) | HTCLIENT
    bh, vh = int(bar.frame(), 16), int(win.frame(), 16)
    assert send(bh, WM_MOUSEACTIVATE, bh, msg) == MA_NOACTIVATE
    assert send(vh, WM_MOUSEACTIVATE, vh, msg) == MA_ACTIVATE
    # the volume OSD floats over the film the same way
    a._flash_osd("Volume 50%", ms=300)
    pump(root)
    oh = int(a._osd_win.frame(), 16)
    assert send(oh, WM_MOUSEACTIVATE, oh, msg) == MA_NOACTIVATE
    a._set_fullscreen(False)
    pump(root, 0.4)
    print("a click on the bar or the volume OSD leaves the film the active "
          "window")


def keys(root, a):
    """Space, Left/Right, F and Esc over the film and the bar. The
    binding table and the handler are checked directly; real key events
    go to whichever window has the focus, which a CI desktop can refuse
    to give, so they run only when the film window has it."""
    win, e = a.video_win, a.embedded
    for w in (win, a.fs_bar):
        for seq in ("<space>", "<Left>", "<Right>", "<KeyPress-f>",
                    "<KeyPress-F>", "<Escape>"):
            assert w.bind(seq), (str(w), seq)
    ev = types.SimpleNamespace(widget=win)
    calls = len(a.fs_calls)
    assert a._on_key("fullscreen", ev) == "break"
    assert a.fullscreen and a.fs_calls[calls][0] == "on", a.fs_calls[calls:]
    assert a._on_key("leave", ev) == "break"
    assert not a.fullscreen and a.fs_calls[-1][0] == "off", a.fs_calls
    assert len(a.fs_calls) == calls + 2, a.fs_calls[calls:]
    pump(root, 0.3)
    assert not shown(a)
    pauses = a.pauses
    assert a._on_key("pause", ev) == "break" and a.pauses == pauses + 1
    e.t, e.n, e.playing = 754.4, 6750.0, True
    seeks = len(e.seeks)
    a._on_key("back", ev)
    a._on_key("forward", ev)
    assert [round(s, 3) for s in e.seeks[seeks:]] == [749.4, 759.4], \
        e.seeks[seeks:]
    print("keys: Space, Left/Right, F and Esc bound on the film window and "
          "the bar; pause, seek 5 s, fullscreen on and off")

    win.deiconify()
    win.focus_force()
    pump(root, 0.3)
    w = win.focus_get()
    if w is None or w.winfo_toplevel() is not win:
        print("SKIPPED real key events: the test window has no focus")
        return
    pauses, seeks = a.pauses, len(e.seeks)
    w.event_generate("<KeyPress-space>")
    w.event_generate("<KeyPress-Right>")
    pump(root)
    assert a.pauses == pauses + 1, (a.pauses, pauses)
    assert len(e.seeks) == seeks + 1 and round(e.seeks[-1], 3) == 759.4, \
        e.seeks[seeks:]
    print("real key events over the film: Space pauses, Right seeks")


def frame_steps(root, a):
    """The bar's -1 frame / +1 frame: a nudge of one frame at the film's
    rate, through the shell (so the offset remembers it), and a film's
    24 fps while libvlc has no rate yet."""
    e = a.embedded
    e.t, e.n, e.playing = 754.4, 6750.0, True
    a._set_fullscreen(True)
    move(root, a, 70, 30)
    assert shown(a)
    a._fs_refresh()
    texts = [b.cget("text") for b in (a._fs_play, a._fs_stop,
                                      a._fs_frame_back, a._fs_frame_fwd,
                                      a._fs_resync, a._fs_full)]
    assert texts == ["Pause", "Stop", "−1 frame", "+1 frame",
                     "Resync here", "Resync (full)"], texts
    a.nudges.clear()
    a._fs_frame_back.invoke()
    a._fs_frame_fwd.invoke()
    pump(root)
    assert a.nudges == [-1 / 25, 1 / 25], a.nudges
    e.rate = 0                                   # libvlc: no picture yet
    a._fs_frame_fwd.invoke()
    pump(root)
    assert a.nudges[-1] == 1 / 24, a.nudges
    e.rate = 25.0
    a._set_fullscreen(False)
    pump(root, 0.3)
    print("+-1 frame: one frame at libvlc's rate (24 fps when it has "
          "none), as a nudge; the bar's buttons read as the board does")


def bar_follows_film(root, a):
    """The bar is placed again when the film moves or changes size - on
    the Mac it can first show while the film is still growing into
    fullscreen."""
    win, bar = a.video_win, a.fs_bar
    a._set_fullscreen(True)
    move(root, a, 80, 30)
    assert shown(a)
    win.geometry("800x450+60+50")
    move(root, a, 81, 30)                        # and keep it up
    pump(root, 0.2)
    assert shown(a)
    mid = bar.winfo_rootx() + bar.winfo_width() // 2
    want = win.winfo_rootx() + win.winfo_width() // 2
    assert abs(mid - want) <= 1, (mid, want)
    assert (win.winfo_rooty() < bar.winfo_rooty()
            and bar.winfo_rooty() + bar.winfo_height()
            < win.winfo_rooty() + win.winfo_height()), \
        (bar.winfo_geometry(), win.winfo_geometry())
    a._set_fullscreen(False)
    win.geometry("960x540+40+40")
    pump(root, 0.3)
    print("the bar follows the film when it moves or changes size")


def external_fullscreen(root, a):
    """Switching to the VLC app while the film is fullscreen leaves
    fullscreen first, then hides the film window: shown again later, it
    must not come back covering the screen with nothing playing."""
    win = a.video_win
    a._set_fullscreen(True)
    move(root, a, 90, 30)
    assert shown(a)
    hwnd = int(win.frame(), 16)
    a.player_var.set("external")
    a._apply_player_choice()
    pump(root)
    assert a.fs_calls[-1] == ("off", hwnd, "kept"), a.fs_calls[-2:]
    assert not a.fullscreen and not shown(a)
    assert win.state() == "withdrawn", win.state()
    a.player_var.set("embedded")
    a._apply_player_choice()
    pump(root)
    assert a.player is a.embedded and win.state() == "normal", win.state()
    assert not a.fullscreen
    print("the VLC app while fullscreen: fullscreen left, the bar gone and "
          "the film window hidden; back to the built-in player, it shows")


def sync_args(root):
    """What the real _sync hands the controller: Resync (full) searches
    the whole film - a blank hint - even with a position hint set, while
    Sync and Resync here go on using it."""
    import tkinter as tk

    from ui import shell, widgets

    calls = []

    def rec(kind):
        return lambda hint, mute=True: calls.append((kind, hint, mute)) or True

    s = types.SimpleNamespace(
        ctl=types.SimpleNamespace(busy=False, sync=rec("sync"),
                                  resync=rec("resync")),
        mute_var=tk.BooleanVar(root, value=False),
        hint_var=tk.StringVar(root, value="1:05:00"),
        sync_btn=widgets.FlatButton(root, "Sync to stream"),
        resync_btn=widgets.FlatButton(root, "Resync"),
        _fs_resync=widgets.FlatButton(root, "Resync here"),
        _fs_full=widgets.FlatButton(root, "Resync (full)"),
        _ready=lambda: True, _sync_state="none",
        _show_sync_state=lambda *a: "")
    s._set_busy = lambda on: shell.Shell._set_busy(s, on)
    for kw in ({}, {"resync": True}, {"whole": True}):
        assert shell.Shell._sync(s, **kw) is True
    # mute_var is off here, so a call that dropped it would show True
    assert calls == [("sync", "1:05:00", False),
                     ("resync", "1:05:00", False),
                     ("sync", "", False)], calls
    # a started sync locks every button that starts one, the fullscreen
    # bar's included; while one runs, another starts nothing
    for b in (s.sync_btn, s.resync_btn, s._fs_resync, s._fs_full):
        assert b.cget("state") == "disabled", b.cget("text")
    s.ctl.busy = True
    assert shell.Shell._sync(s) is False and len(calls) == 3, calls
    print("resync (full) searches the whole film even with a hint set; "
          "sync and resync here keep the hint; a started sync locks the "
          "bar's buttons too, and none starts while one runs")


def monitors(root):
    """Fullscreen covers the monitor the window is on - every monitor here,
    not only the primary one Tk's -fullscreen always picks - keeps the
    window's hwnd (libvlc draws into it) and puts it back exactly. Each
    monitor is covered for a moment."""
    import tkinter as tk

    import windowctl

    rects = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                        ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    def each(_hmon, _hdc, r, _lp):
        r = r.contents
        rects.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, each, 0)
    w = tk.Toplevel(root)
    try:
        for mx, my, mw, mh in rects:
            w.geometry(f"320x180+{mx + 60}+{my + 60}")
            pump(root, 0.2)
            hwnd = int(w.frame(), 16)
            before = windowctl.window_rect(hwnd)
            assert windowctl.monitor_rect(hwnd) == (mx, my, mw, mh)
            saved = windowctl.borderless_fullscreen(hwnd)
            pump(root, 0.1)
            assert windowctl.window_rect(hwnd) == (mx, my, mw, mh), \
                (windowctl.window_rect(hwnd), (mx, my, mw, mh))
            assert int(w.frame(), 16) == hwnd
            windowctl.unfullscreen(hwnd, saved)
            pump(root, 0.1)
            assert windowctl.window_rect(hwnd) == before, \
                (windowctl.window_rect(hwnd), before)
    finally:
        w.destroy()
    print(f"fullscreen covers the window's own monitor, on each of "
          f"{len(rects)} here, and restores it, same hwnd")


def vout_hits(root, a):
    """The bindings above only matter if clicks reach them. With mouse
    input off, libvlc creates its vout window disabled, and Windows'
    hit-test passes a disabled child by - so a click over the playing
    picture lands on our frame. (WS_EX_TRANSPARENT, which an earlier shell
    swept onto the vout for this, does not change hit-testing at all.)
    The player is made the app's own way (the adapter's _make_player)."""
    import subprocess

    import imageio_ffmpeg

    import players

    a.video_win.geometry("320x180+40+40")
    a.video_win.deiconify()
    pump(root, 0.3)
    try:
        p = a._make_player()
    except players.VLCError as e:
        print(f"SKIPPED vout hit-test: no VLC here ({str(e)[:60]}...)")
        return
    u32 = ctypes.windll.user32
    u32.WindowFromPoint.argtypes = [wintypes.POINT]
    u32.WindowFromPoint.restype = wintypes.HWND
    u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    parent = a.video_frame.winfo_id()
    kids = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, ctypes.c_void_p)
    def collect(h, _lp):
        kids.append(h)
        return True

    pt = wintypes.POINT(a.video_frame.winfo_rootx() + 160,
                        a.video_frame.winfo_rooty() + 90)

    def covers(h):
        r = wintypes.RECT()
        u32.GetWindowRect(h, ctypes.byref(r))
        return r.left <= pt.x < r.right and r.top <= pt.y < r.bottom

    with tempfile.TemporaryDirectory() as tmp:
        still = os.path.join(tmp, "still.png")
        # one flat blue frame, made by the ffmpeg the app already ships
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner",
                        "-loglevel", "error", "-f", "lavfi",
                        "-i", "color=c=0x285aa0:s=640x360", "-frames:v", "1",
                        still], check=True, capture_output=True)
        try:
            p.load(still)
            p.mp.play()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                pump(root, 0.2)
                kids.clear()
                u32.EnumChildWindows(parent, collect, 0)
                if any(covers(h) for h in kids):
                    break
            assert any(covers(h) for h in kids), \
                "libvlc never put a vout over the picture"
            hit = u32.WindowFromPoint(pt) or 0
            assert hit == parent, (f"a click over the picture goes to "
                                   f"{hex(hit)}, not our frame {hex(parent)}")
        finally:
            p.stop()
            pump(root, 0.3)
    print(f"real libvlc: {len(kids)} vout window(s) over the picture, and "
          "a click there still lands on our frame")


def main():
    wheel_steps()
    if sys.platform != "win32":
        print(f"SKIPPED: the Windows film window, this is {sys.platform} "
              "(the film window's Mac controls come in Step 5)")
        return
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    # an exception in any Tk callback fails the test, not just a log line
    errors = []
    root.report_callback_exception = (
        lambda *exc: errors.append("".join(traceback.format_exception(*exc))))
    try:
        a = make_app(root)
        mouse_controls(root, a)
        # The bar's checks need a window big enough for the whole bar, over
        # much of the screen - and the real pointer must not rest on it:
        # Windows sends a move at the pointer whenever a window appears or
        # vanishes under it, which would mix with the checks' own moves.
        pt = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        ctypes.windll.user32.SetCursorPos(0, 0)     # the window is at +40+40
        try:
            fs_bar(root, a)
            no_activate(root, a)
            keys(root, a)
            frame_steps(root, a)
            bar_follows_film(root, a)
            external_fullscreen(root, a)
        finally:
            ctypes.windll.user32.SetCursorPos(pt.x, pt.y)
        sync_args(root)
        monitors(root)
        vout_hits(root, a)
        pump(root, 0.2)
        assert not errors, "\n".join(errors)
    finally:
        root.destroy()
    print("VIDEOMOUSE TEST PASSED")


if __name__ == "__main__":
    main()
