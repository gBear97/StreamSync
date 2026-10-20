"""Live check of windowctl: spawn a real window, find, minimize, restore,
and read the program it belongs to. Before that, find_stream_window over
a made-up list of windows: what Automatic picks; and placement() and
put_placement() on a real Tk window hidden and shown again, as the stream
swap hides and shows the film."""

import ctypes
import os
import subprocess
import sys
import tempfile
import time

TITLE = "WinCtlTest-9271"


def stream_window_fallback(windowctl):
    """Automatic's pick: a Twitch or Kick title first, else the browser
    window if it is the only one - a VOD's tab is named after the VOD,
    not Twitch. Of several browser windows none is guessed at. A saved
    title and a pinned window that is still open come before all that."""
    saved = windowctl.list_windows, windowctl.window_exe, windowctl.is_valid

    def desktop(*wins):              # (hwnd, title, program)
        windowctl.list_windows = lambda: [(h, t) for h, t, _exe in wins]
        exes = {h: exe for h, _t, exe in wins}
        windowctl.window_exe = lambda h: exes.get(h, "")

    vod = (11, "Day 3 of the marathon - Google Chrome", "chrome.exe")
    others = [(12, "Downloads", "explorer.exe"), (13, "StreamSync", "python.exe"),
              (14, "notes.txt - Notepad", "notepad.exe")]
    windowctl.is_valid = lambda h: h == 99
    try:
        desktop(vod, *others)
        assert windowctl.find_stream_window() == 11, "the lone browser wasn't picked"
        desktop(*others, (21, "A film night - Personal - Microsoft\u200b Edge", "msedge.exe"))
        assert windowctl.find_stream_window() == 21, "a lone Edge wasn't picked"
        desktop(vod, *others, (22, "Inbox \u2014 Mozilla Firefox", "firefox.exe"))
        assert windowctl.find_stream_window() is None, "guessed between two browsers"
        desktop(vod, *others, (23, "Picture in picture", "chrome.exe"))
        assert windowctl.find_stream_window() is None, "guessed between two windows"
        desktop(vod, *others, (24, "streamer - Twitch \u2014 Mozilla Firefox",
                               "firefox.exe"))
        assert windowctl.find_stream_window() == 24, "Twitch lost to the browser rule"
        desktop(vod, *others)
        assert windowctl.find_stream_window(None, "Notes.txt") == 14, "saved title lost"
        assert windowctl.find_stream_window(99, "Notes.txt") == 99, "pinned pick lost"
        desktop(*others)
        assert windowctl.find_stream_window() is None, "picked a window with no browser"
        print("Automatic: Twitch/Kick title, else the one browser window; "
              "two browsers or two of one browser's windows: no guess")
    finally:
        windowctl.list_windows, windowctl.window_exe, windowctl.is_valid = saved


def film_placement(windowctl):
    """The stream swap reads the film window's placement before Tk's
    withdraw and puts it back after its deiconify: the window must be
    where it was - moved and sized by Windows (a drag, not Tk), maximized,
    and snapped to the right half of its monitor, where this desktop lets
    a test press Win+Right (the snap is skipped otherwise). What Tk alone
    does to a snapped window is printed, not asserted."""
    import tkinter as tk
    from ctypes import wintypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # as the app runs
    except Exception:
        pass
    u32 = ctypes.windll.user32
    root = tk.Tk()
    root.title("PlacementTest-9271")
    try:
        root.geometry("480x270+120+120")
        root.update()
        hwnd = int(root.frame(), 16)
        hw = wintypes.HWND(hwnd)

        def settle():
            for _ in range(5):
                root.update()
                time.sleep(0.05)

        def hide_and_show(put):
            saved = windowctl.placement(hwnd)
            root.withdraw()
            settle()
            root.deiconify()
            root.update_idletasks()
            if put:
                windowctl.put_placement(hwnd, saved)
            settle()

        u32.SetWindowPos(hw, None, 200, 150, 640, 360,
                         windowctl.SWP_NOZORDER | windowctl.SWP_NOACTIVATE)
        settle()
        want = windowctl.window_rect(hwnd)
        assert want[2:] == (640, 360), want
        hide_and_show(True)
        assert windowctl.window_rect(hwnd) == want, (windowctl.window_rect(hwnd), want)

        u32.ShowWindow(hw, 3)                  # SW_MAXIMIZE
        settle()
        assert u32.IsZoomed(hw)
        want = windowctl.window_rect(hwnd)
        hide_and_show(True)
        assert u32.IsZoomed(hw), "came back not maximized"
        assert windowctl.window_rect(hwnd) == want, (windowctl.window_rect(hwnd), want)
        u32.ShowWindow(hw, windowctl.SW_RESTORE)
        settle()
        assert windowctl.window_rect(hwnd) == (200, 150, 640, 360), \
            windowctl.window_rect(hwnd)
        print("placement: a window moved by Windows, and one maximized, come "
              "back where they were from withdraw and deiconify")

        before = windowctl.window_rect(hwnd)
        snapped = snap_right(windowctl, root, hwnd)
        if snapped is None:
            print("SKIPPED snapped placement: this desktop would not let the "
                  "test snap a window (no keyboard focus, or no snap)")
            return
        root.deiconify()                       # as entering fullscreen does
        settle()
        shown_kept = windowctl.window_rect(hwnd) == snapped
        hide_and_show(False)
        tk_kept = windowctl.window_rect(hwnd) == snapped
        print(f"snapped {before} to {snapped}; Tk's deiconify of the window "
              f"shown: {'kept the snap' if shown_kept else 'lost it'}; "
              f"withdraw and deiconify alone: "
              f"{'kept it' if tk_kept else f'lost it, back at {windowctl.window_rect(hwnd)}'}")
        if not shown_kept or not tk_kept:
            snapped = snap_right(windowctl, root, hwnd)
            assert snapped is not None, "the window would not snap a second time"
        hide_and_show(True)
        assert windowctl.window_rect(hwnd) == snapped, \
            (windowctl.window_rect(hwnd), snapped)
        print("placement: a snapped window comes back at its snapped rectangle")
    finally:
        root.destroy()


def snap_right(windowctl, root, hwnd):
    """Win+Right on the window, if it has the keyboard (a key sent to
    another window would snap that one instead). Its rectangle once
    snapped, or None if it never got the keyboard or never snapped: its
    placement's normal rectangle (where it restores to) stays apart from
    the rectangle it is at."""
    u32 = ctypes.windll.user32
    u32.SetForegroundWindow(hwnd)
    for _ in range(10):
        root.update()
        time.sleep(0.05)
    if windowctl.foreground() != hwnd:
        return None

    def apart():
        wp, rect = windowctl.placement(hwnd)
        r = wp.rcNormalPosition
        return (r.left, r.top, r.right - r.left, r.bottom - r.top) != rect

    VK_LWIN, VK_RIGHT, KEYEVENTF_KEYUP = 0x5B, 0x27, 0x0002
    try:
        u32.keybd_event(VK_LWIN, 0, 0, 0)
        u32.keybd_event(VK_RIGHT, 0, 0, 0)
        u32.keybd_event(VK_RIGHT, 0, KEYEVENTF_KEYUP, 0)
    finally:
        u32.keybd_event(VK_LWIN, 0, KEYEVENTF_KEYUP, 0)
    deadline = time.time() + 3
    while time.time() < deadline and not apart():
        root.update()
        time.sleep(0.05)
    # the snap animates: wait for the rectangle to stop changing
    last = None
    while time.time() < deadline and windowctl.window_rect(hwnd) != last:
        last = windowctl.window_rect(hwnd)
        root.update()
        time.sleep(0.15)
    return windowctl.window_rect(hwnd) if apart() else None


def main():
    if sys.platform != "win32":
        print(f"SKIPPED: windowctl is Windows-only, this is {sys.platform} "
              "(use test_macwindowctl.py on macOS)")
        return
    import windowctl  # touches ctypes.windll, so only after the guard

    stream_window_fallback(windowctl)
    film_placement(windowctl)

    # The window's process says when its window is up: it creates `shown`
    # once update() has mapped it. On a CI runner the part that varies is
    # Tk() itself: usually under a second, now and then far longer - two
    # runs failed at the old 10 s limit with the rest of their jobs at
    # normal speed. That is the runner, not windowctl, so opening gets a
    # minute. Finding the window, which is what this tests, still gets 5 s
    # from when it is up; on the runners it took under 50 ms.
    shown = os.path.join(tempfile.mkdtemp(), "shown")
    proc = subprocess.Popen([
        sys.executable, "-c",
        f"import tkinter as tk; r = tk.Tk(); r.title('{TITLE}'); "
        f"r.geometry('200x100+50+50'); r.update(); open({shown!r}, 'w')"
        ".close(); r.mainloop()",
    ])
    try:
        deadline = time.time() + 60
        while not os.path.exists(shown):
            assert proc.poll() is None, \
                f"the test window's process exited ({proc.returncode})"
            assert time.time() < deadline, "the test window never opened"
            time.sleep(0.05)
        hwnd = None
        deadline = time.time() + 5
        while time.time() < deadline and hwnd is None:
            for h, t in windowctl.list_windows():
                if t == TITLE:
                    hwnd = h
                    break
            time.sleep(0.2)
        assert hwnd, "spawned window never appeared in list_windows()"
        assert windowctl.is_valid(hwnd)
        assert windowctl.find_by_pid(proc.pid) == hwnd
        exe = windowctl.window_exe(hwnd)
        assert exe == os.path.basename(sys.executable).lower(), exe
        assert windowctl.window_exe(0) == "", "a window that isn't there has a program"
        print(f"the spawned window's program read: {exe}")

        windowctl.minimize(hwnd)
        time.sleep(0.4)
        assert windowctl.is_minimized(hwnd), "window did not minimize"
        windowctl.restore(hwnd)
        time.sleep(0.4)
        assert not windowctl.is_minimized(hwnd), "window did not restore"
        print("hwnd found by title and pid; minimize + restore both verified")
        # restore() asks for the foreground, which Windows may refuse;
        # whatever has it, foreground() names a live window (or 0: none)
        fg = windowctl.foreground()
        assert isinstance(fg, int) and (fg == 0 or windowctl.is_valid(fg)), fg
        print(f"foreground window read: {hex(fg)}")
        print("WINDOWCTL TEST PASSED")
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
