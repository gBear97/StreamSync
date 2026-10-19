"""Live check of windowctl: spawn a real window, find, minimize, restore,
and read the program it belongs to. Before that, find_stream_window over
a made-up list of windows: what Automatic picks."""

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


def main():
    if sys.platform != "win32":
        print(f"SKIPPED: windowctl is Windows-only, this is {sys.platform} "
              "(use test_macwindowctl.py on macOS)")
        return
    import windowctl  # touches ctypes.windll, so only after the guard

    stream_window_fallback(windowctl)

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
