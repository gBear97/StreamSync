"""Live check of the Windows video window's mouse controls.

Real Tk events on a real (small) video window: double-click toggles
fullscreen, right-click opens the player menu, the wheel steps the volume
without ever unmuting, and in fullscreen a moving mouse brings up the
control bar (timeline, play/pause, stop, resync here / full) that hides
again, with the pointer, once the mouse rests. Fullscreen itself is
checked on every monitor attached: it must cover the monitor the window
is on. Then, where VLC is installed, real libvlc plays a still image into
the window, and a click over the picture must land on our frame - where
those bindings are - not on libvlc's vout.

No config: the app object is built from its video-window parts only, so
nothing here can load a film or save ~/.streamsync.json.
"""

import ctypes
import os
import sys
import tempfile
import time
import types
from ctypes import wintypes


class FakeEmbedded:
    def __init__(self):
        self.vol = None
        self.mute_calls = 0
        self.tracks = [(-1, "Disable"), (2, "English"), (3, "Francais")]
        self.current = 2
        self.picked = []
        self.t, self.n, self.playing = None, None, False
        self.seeks, self.stops = [], 0

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

    import app
    import windowctl

    # The frame-style swap is recorded, not done: the bar's checks must
    # not take the screen over for seconds at a time (monitors() checks
    # the real swap, a moment per monitor).
    fs_calls = []
    app.windowctl = types.SimpleNamespace(
        borderless_fullscreen=lambda h: fs_calls.append(("on", h)) or "kept",
        unfullscreen=lambda h, saved: fs_calls.append(("off", h, saved)),
        no_activate_on_click=windowctl.no_activate_on_click)

    class FakeApp(app.App):
        """App with only the video window built - see the module note."""

        def __init__(self, root):
            self.root = root
            self.fullscreen = False
            self._closing = False
            self._osd_win = None
            self._osd_after = None
            self._fs_saved = None
            self.mute_var = tk.BooleanVar(value=True)
            self.method_var = tk.StringVar(value="audio")
            self.status_lbl = tk.Label(root)
            self.embedded = FakeEmbedded()
            self.ctl = types.SimpleNamespace(player=self.embedded,
                                             external=None, busy=False)
            self.fs_calls, self.pauses, self.popups = fs_calls, 0, []
            self.syncs = []
            self._build_video_window()
            self._build_video_menu()
            self._build_fs_bar()
            # tk_popup runs a modal menu loop that would wait for a human
            self.video_menu.tk_popup = lambda x, y: self.popups.append((x, y))

        def _toggle_pause(self):
            self.pauses += 1

        def _on_mute_toggle(self):
            pass

        def _sync(self, resync=False, whole=False):
            # recorded; what the real one sends is sync_args()'s check
            self.syncs.append((resync, whole))
            self.ctl.busy = True

    return FakeApp(root)


def pump(root, seconds=0.15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.01)


def click(widget, seq, **kw):
    widget.event_generate(seq, x=20, y=20, **kw)


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
    assert labels == ["Play/Pause", "Fullscreen", "Mute local audio",
                      "Volume up", "Volume down", "Subtitles",
                      "Hide video window"], labels
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

    # wheel: +/-5 from 100 when libvlc has no volume yet, clamped to
    # 0-125, and never unmutes; the OSD says so
    click(frame, "<MouseWheel>", delta=120)
    pump(root)
    assert a.embedded.vol == 105, a.embedded.vol
    assert a._osd_lbl.cget("text") == "Volume 105% (muted)"
    assert a.mute_var.get() is True and a.embedded.mute_calls == 0
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
    print("wheel: volume steps by 5, clamped to 0-125, stays muted; the "
          "OSD shows it and hides itself")

    # external VLC: the video window is not its player - no menu, no wheel
    a.ctl.player = object()
    click(frame, "<ButtonPress-3>", button=3, rootx=1, rooty=1)
    click(frame, "<MouseWheel>", delta=120)
    pump(root)
    assert len(a.popups) == 1 and a.embedded.vol == 0
    a.ctl.player = a.embedded
    print("external player: right-click and wheel leave the embed alone")


LRESULT = ctypes.c_ssize_t
WM_MOUSEACTIVATE, WM_LBUTTONDOWN, HTCLIENT = 0x0021, 0x0201, 1
MA_ACTIVATE, MA_NOACTIVATE = 1, 3


def fs_bar(root, a):
    """The fullscreen control bar, over the small window standing in for a
    fullscreen one (the frame-style swap is stubbed - see make_app)."""
    send = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                              wintypes.WPARAM, wintypes.LPARAM)(
        ("SendMessageW", ctypes.windll.user32))
    win, frame, bar, e = a.video_win, a.video_frame, a.fs_bar, a.embedded
    a.FS_BAR_IDLE = 0.4
    a._pointer_on_fs_bar = lambda: False   # wherever the real pointer is
    e.t, e.n, e.playing = 754.4, 6750.0, True

    def move(x, y):
        frame.event_generate("<Motion>", x=x, y=y, rootx=500 + x,
                             rooty=300 + y)
        pump(root, 0.1)

    def shown():
        return a._fs_up and bar.winfo_viewable()

    move(10, 10)
    assert not a._fs_up and bar.state() == "withdrawn"
    a._set_fullscreen(True)
    move(12, 10)
    assert shown()
    assert a._fs_play.cget("text").endswith("Pause")
    assert a._fs_time.cget("text") == "12:34", a._fs_time.cget("text")
    assert a._fs_len.cget("text") == "1:52:30", a._fs_len.cget("text")
    mid = bar.winfo_rootx() + bar.winfo_width() // 2
    want = win.winfo_rootx() + win.winfo_width() // 2
    assert abs(mid - want) <= 1, (mid, want)
    assert (bar.winfo_rooty() + bar.winfo_height()
            < win.winfo_rooty() + win.winfo_height())
    print("fullscreen: a moving mouse brings up the bar, centred at the "
          "bottom, reading 12:34 of 1:52:30")

    # a click must not make the bar the active window - the film keeps
    # the keyboard (Space, Esc) and the taskbar stays under it. The video
    # window, plain Tk, is the control: Tk says yes to activation.
    msg = (WM_LBUTTONDOWN << 16) | HTCLIENT
    bh, vh = int(bar.frame(), 16), int(win.frame(), 16)
    assert send(bh, WM_MOUSEACTIVATE, bh, msg) == MA_NOACTIVATE
    assert send(vh, WM_MOUSEACTIVATE, vh, msg) == MA_ACTIVATE
    print("a click on the bar leaves the film the active window")

    pump(root, 0.8)
    assert not shown() and bar.state() == "withdrawn"
    assert frame.cget("cursor") == "none" and win.cget("cursor") == "none"
    move(12, 10)              # what Windows sends as the bar vanishes
    assert not shown() and frame.cget("cursor") == "none"
    move(40, 30)
    assert shown() and frame.cget("cursor") == ""
    print("a resting mouse hides the bar and the pointer; a repeated "
          "position does not bring them back, a real move does")

    # the timeline: a click seeks there, a drag seeks once, on release
    a._fs_refresh()
    pump(root)
    tr = a._fs_track
    x0, span = 8, tr.winfo_width() - 16
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
    assert a._fs_play.cget("text").endswith("Play")
    assert a._fs_time.cget("text") == "-:--"
    tr.event_generate("<ButtonPress-1>", x=x0 + 10, y=10)
    tr.event_generate("<ButtonRelease-1>", x=x0 + 10, y=10)
    pump(root)
    assert len(e.seeks) == 2, e.seeks            # stopped: nothing to seek
    print("play/pause and stop work; a stopped film has no timeline")

    # resync: the bar stays up, with its status, until the result is in;
    # either button locks both, and the one pressed says it is running
    resync, full = a._fs_resync, a._fs_full
    for btn, want in ((resync, (True, False)), (full, (False, True))):
        label = btn.cget("text")
        move(50 + len(a.syncs), 30)
        assert shown()
        btn.invoke()
        pump(root)
        assert a.syncs[-1] == want, a.syncs
        assert resync.cget("state") == full.cget("state") == "disabled"
        assert btn.cget("text").endswith("Resyncing...")
        a._set_status("Listening to the stream (12 s)...")
        assert a._fs_status.cget("text") == \
            "Listening to the stream (12 s)..."
        pump(root, 0.8)
        assert shown()
        a.ctl.busy = False
        a._fs_sync_done()
        assert resync.cget("state") == full.cget("state") == "normal"
        assert btn.cget("text") == label
        pump(root, 0.2)
        assert shown()
        pump(root, 0.6)
        assert not shown()
    assert a.syncs == [(True, False), (False, True)], a.syncs
    print("resync here / resync (full): the bar holds while one runs, "
          "shows its status, then rests as usual")

    # gone with whatever takes the film away
    move(41, 30)
    win.withdraw()
    pump(root, 0.4)
    assert not shown()
    win.deiconify()
    move(42, 30)
    assert shown()
    a._set_fullscreen(False)
    assert not shown() and frame.cget("cursor") == ""
    move(43, 30)
    assert not shown()                           # windowed: no bar
    a._set_fullscreen(True)
    a.ctl.player = object()                      # external VLC
    move(44, 30)
    assert not shown()
    a.ctl.player = e
    a._closing = True
    a._show_fs_bar()
    assert not shown()
    a._closing = False
    a._set_fullscreen(False)
    print("the bar goes when the film does, leaves with fullscreen, and "
          "never shows windowed, for external VLC or while closing")


def sync_args():
    """What the real _sync hands the controller: Resync (full) searches
    the whole film - a blank hint - even with a position hint set, while
    Sync and Resync here go on using it."""
    import tkinter as tk
    from tkinter import ttk

    import app

    calls = []

    def rec(kind):
        return lambda hint, window, method, **video: calls.append(
            (kind, hint, window, method)) or True

    s = types.SimpleNamespace(
        ctl=types.SimpleNamespace(busy=False, sync=rec("sync"),
                                  resync=rec("resync")),
        method_var=tk.StringVar(value="audio"),
        mute_var=tk.BooleanVar(value=True),
        hint_var=tk.StringVar(value="1:05:00"),
        window_var=tk.StringVar(value="2:00"),
        sync_btn=ttk.Button(), resync_btn=ttk.Button(),
        _ready=lambda need_region: True)
    for kw in ({}, {"resync": True}, {"whole": True}):
        app.App._sync(s, **kw)
    assert calls == [("sync", "1:05:00", "2:00", "audio"),
                     ("resync", "1:05:00", "2:00", "audio"),
                     ("sync", "", "2:00", "audio")], calls
    print("resync (full) searches the whole film even with a hint set; "
          "sync and resync here keep the hint")


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
    swept onto the vout for this, does not change hit-testing at all.)"""
    import ctypes
    from ctypes import wintypes

    from PIL import Image

    import players

    try:
        p = players.EmbeddedPlayer(a.video_frame.winfo_id())
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
        Image.new("RGB", (640, 360), (40, 90, 160)).save(still)
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
    if sys.platform != "win32":
        print(f"SKIPPED: the Windows video window, this is {sys.platform} "
              "(the Mac shell is mac_app.py)")
        return
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    try:
        a = make_app(root)
        mouse_controls(root, a)
        fs_bar(root, a)
        sync_args()
        monitors(root)
        vout_hits(root, a)
    finally:
        root.destroy()
    print("VIDEOMOUSE TEST PASSED")


if __name__ == "__main__":
    main()
