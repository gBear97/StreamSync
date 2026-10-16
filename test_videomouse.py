"""Live check of the Windows video window's mouse controls.

Real Tk events on a real (small) video window: double-click toggles
fullscreen, right-click opens the player menu, the wheel steps the volume
without ever unmuting. Then, where VLC is installed, real libvlc plays a
still image into the window, and a click over the picture must land on
our frame - where those bindings are - not on libvlc's vout.

No config: the app object is built from its video-window parts only, so
nothing here can load a film or save ~/.streamsync.json.
"""

import os
import sys
import tempfile
import time
import types


class FakeEmbedded:
    def __init__(self):
        self.vol = None
        self.mute_calls = 0
        self.tracks = [(-1, "Disable"), (2, "English"), (3, "Francais")]
        self.current = 2
        self.picked = []

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

    class FakeApp(app.App):
        """App with only the video window built - see the module note."""

        def __init__(self, root):
            self.root = root
            self.fullscreen = False
            self._closing = False
            self._osd_win = None
            self._osd_after = None
            self.mute_var = tk.BooleanVar(value=True)
            self.embedded = FakeEmbedded()
            self.ctl = types.SimpleNamespace(player=self.embedded,
                                             external=None)
            self.fs_calls, self.pauses, self.popups = [], 0, []
            self._build_video_window()
            self._build_video_menu()
            # tk_popup runs a modal menu loop that would wait for a human
            self.video_menu.tk_popup = lambda x, y: self.popups.append((x, y))

        def _set_fullscreen(self, flag):
            # recorded, not done: a test must not take over the screen
            self.fs_calls.append(flag)
            self.fullscreen = flag

        def _toggle_pause(self):
            self.pauses += 1

        def _on_mute_toggle(self):
            pass

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

    # double-click on the picture (the frame: clicks land there once the
    # vout is shielded) toggles fullscreen, and again toggles it back.
    # Tk pairs presses by their event time, not the wall clock, so each
    # press is stamped: 100 ms apart within a double-click, 2 s between.
    t = 1000
    for want in (True, False):
        for _ in range(2):
            t += 100
            click(frame, "<ButtonPress-1>", button=1, time=t)
            click(frame, "<ButtonRelease-1>", button=1, time=t + 20)
        t += 2000
        pump(root)
        assert a.fs_calls and a.fs_calls[-1] is want, a.fs_calls
    assert a.fs_calls == [True, False], a.fs_calls
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
        vout_hits(root, a)
    finally:
        root.destroy()
    print("VIDEOMOUSE TEST PASSED")


if __name__ == "__main__":
    main()
