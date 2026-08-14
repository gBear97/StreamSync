"""Find, minimize and restore other applications' windows (Win32), and
put our own video window fullscreen on the right monitor.

Used to bring the stream's browser window into view while the streamer
has the film paused, then tuck it away again when the film resumes.
"""

import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32

SW_MINIMIZE = 6
SW_RESTORE = 9
SW_SHOWNOACTIVATE = 4
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

GWL_STYLE = -16
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_POPUP = 0x80000000
SWP_FRAMECHANGED = 0x0020
SWP_NOZORDER = 0x0004
MONITOR_DEFAULTTONEAREST = 2


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


def monitor_rect(hwnd):
    """(x, y, w, h) of the monitor this window sits on, nearest if off-screen."""
    hmon = user32.MonitorFromWindow(wintypes.HWND(hwnd),
                                    MONITOR_DEFAULTTONEAREST)
    mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    r = mi.rcMonitor
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def window_rect(hwnd):
    rc = wintypes.RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rc))
    return (rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top)


def borderless_fullscreen(hwnd):
    """Cover the window's current monitor, returning state for restore().

    Tk's own -fullscreen always lands on the primary display, and
    toggling wm overrideredirect makes Tk recreate the OS window - which
    would pull the drawable out from under libvlc. Swapping the frame
    style in place keeps the hwnd alive.
    """
    h = wintypes.HWND(hwnd)
    saved = (user32.GetWindowLongW(h, GWL_STYLE), window_rect(hwnd))
    x, y, w, ht = monitor_rect(hwnd)
    user32.SetWindowLongW(h, GWL_STYLE,
                          (saved[0] & ~WS_OVERLAPPEDWINDOW) | WS_POPUP)
    user32.SetWindowPos(h, None, x, y, w, ht,
                        SWP_FRAMECHANGED | SWP_NOZORDER)
    return saved


def unfullscreen(hwnd, saved):
    """Undo borderless_fullscreen() with the state it handed back."""
    style, (x, y, w, ht) = saved
    h = wintypes.HWND(hwnd)
    user32.SetWindowLongW(h, GWL_STYLE, style)
    user32.SetWindowPos(h, None, x, y, w, ht,
                        SWP_FRAMECHANGED | SWP_NOZORDER)


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


def show_noactivate(hwnd):
    """Show/un-minimize WITHOUT giving the window keyboard focus - for
    revealing a player while the user may be typing somewhere else."""
    user32.ShowWindow(wintypes.HWND(hwnd), SW_SHOWNOACTIVATE)


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT),
                ("showCmd", wintypes.UINT),
                ("ptMinPosition", wintypes.POINT),
                ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", wintypes.RECT)]


def get_placement(hwnd):
    """Opaque snapshot of where a window is, including maximized state.
    Feed it back to set_placement() to put the window back exactly."""
    wp = WINDOWPLACEMENT(length=ctypes.sizeof(WINDOWPLACEMENT))
    user32.GetWindowPlacement(wintypes.HWND(hwnd), ctypes.byref(wp))
    return wp


def set_placement(hwnd, wp):
    user32.SetWindowPlacement(wintypes.HWND(hwnd), ctypes.byref(wp))


def fill_monitor(hwnd, rect):
    """Cover the given monitor rect with this window, raised but NOT
    activated - the user may be typing in another app. Capture
    get_placement() first; set_placement() undoes everything.

    A maximized window must be un-maximized first: SetWindowPos cannot
    move one (Windows re-maximizes it on the target monitor), and
    restoring a placement onto a still-maximized window is a positional
    no-op - measured: a browser maximized on monitor B ended up stranded
    on monitor A. Normal-first, the placement round-trips, maximized
    state included."""
    x, y, w, h = rect
    hw = wintypes.HWND(hwnd)
    if user32.IsIconic(hw) or user32.IsZoomed(hw):
        user32.ShowWindow(hw, SW_SHOWNOACTIVATE)
    user32.SetWindowPos(hw, None, x, y, w, h,
                        SWP_NOACTIVATE | SWP_SHOWWINDOW)


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
