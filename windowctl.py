"""Find, minimize and restore other applications' windows (Win32).

Used to bring the stream's browser window into view while the streamer
has the film paused, then tuck it away again when the film resumes.
"""

import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32

SW_MINIMIZE = 6
SW_RESTORE = 9


def list_windows():
    """[(hwnd, title)] for visible, titled top-level windows."""
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                out.append((int(hwnd), buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return out


def is_valid(hwnd):
    return bool(hwnd) and bool(user32.IsWindow(wintypes.HWND(hwnd)))


def is_minimized(hwnd):
    return bool(user32.IsIconic(wintypes.HWND(hwnd)))


def restore(hwnd):
    """Un-minimize and bring to front (foreground is best-effort)."""
    h = wintypes.HWND(hwnd)
    user32.ShowWindow(h, SW_RESTORE)
    user32.BringWindowToTop(h)
    user32.SetForegroundWindow(h)


def minimize(hwnd):
    user32.ShowWindow(wintypes.HWND(hwnd), SW_MINIMIZE)


def find_by_pid(pid):
    """Best top-level window belonging to a process id."""
    best = None
    for hwnd, title in list_windows():
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(owner))
        if owner.value == pid:
            if "vlc" in title.lower():
                return hwnd
            if best is None:
                best = hwnd
    return best


def find_stream_window(preferred_hwnd=None, title_hint=""):
    """The stream's browser window: the pinned pick if still alive, else the
    saved title, else anything that looks like Twitch/Kick."""
    if preferred_hwnd and is_valid(preferred_hwnd):
        return preferred_hwnd
    wins = list_windows()
    hint = (title_hint or "").strip().lower()
    if hint:
        for hwnd, title in wins:
            if hint in title.lower():
                return hwnd
    for hwnd, title in wins:
        t = title.lower()
        if "twitch" in t or "kick.com" in t:
            return hwnd
    return None
