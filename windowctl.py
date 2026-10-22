"""Find, minimize and restore other applications' windows (Win32), and
put our own video window fullscreen on the right monitor.

Used to bring the stream's browser window into view while the streamer
has the film paused, then tuck it away again when the film resumes - and
to put the film's window back exactly where it was when it comes back.
"""

import ctypes
import os
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

SW_SHOWMAXIMIZED = 3
SW_SHOWNOACTIVATE = 4
SW_MINIMIZE = 6
SW_RESTORE = 9

GWL_STYLE = -16
GWLP_WNDPROC = -4
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_POPUP = 0x80000000
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
MONITOR_DEFAULTTONEAREST = 2
WM_MOUSEACTIVATE = 0x0021
MA_NOACTIVATE = 3
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# The programs a stream is watched in, known by their process's file name:
# their window titles end in a dozen ways (Edge's "Microsoft Edge" holds a
# zero-width space, Firefox's dash is an em dash).
BROWSER_EXES = frozenset((
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "vivaldi.exe", "chromium.exe", "arc.exe", "librewolf.exe", "waterfox.exe",
    "floorp.exe", "zen.exe"))


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT),
                ("showCmd", wintypes.UINT),
                ("ptMinPosition", wintypes.POINT),
                ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", wintypes.RECT)]


LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


def _proto(name, restype, *argtypes, dll=user32):
    # Typed privately: windll.user32 is one object for the whole process,
    # so setting argtypes on it would change these calls for everyone.
    return ctypes.WINFUNCTYPE(restype, *argtypes)((name, dll))


_MonitorFromWindow = _proto("MonitorFromWindow", wintypes.HMONITOR,
                            wintypes.HWND, wintypes.DWORD)
_GetMonitorInfoW = _proto("GetMonitorInfoW", wintypes.BOOL,
                          wintypes.HMONITOR, ctypes.POINTER(MONITORINFO))
# styles are bit patterns: read and written unsigned, so WS_POPUP fits
_GetWindowLongW = _proto("GetWindowLongW", wintypes.DWORD,
                         wintypes.HWND, ctypes.c_int)
_SetWindowLongW = _proto("SetWindowLongW", wintypes.DWORD,
                         wintypes.HWND, ctypes.c_int, wintypes.DWORD)
_SetWindowLongPtrW = _proto("SetWindowLongPtrW", ctypes.c_void_p,
                            wintypes.HWND, ctypes.c_int, ctypes.c_void_p)
_GetWindowPlacement = _proto("GetWindowPlacement", wintypes.BOOL,
                             wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT))
_SetWindowPlacement = _proto("SetWindowPlacement", wintypes.BOOL,
                             wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT))
_GetForegroundWindow = _proto("GetForegroundWindow", wintypes.HWND)
_CallWindowProcW = _proto("CallWindowProcW", LRESULT, ctypes.c_void_p,
                          wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                          wintypes.LPARAM)
_OpenProcess = _proto("OpenProcess", wintypes.HANDLE, wintypes.DWORD,
                      wintypes.BOOL, wintypes.DWORD, dll=kernel32)
_QueryFullProcessImageNameW = _proto(
    "QueryFullProcessImageNameW", wintypes.BOOL, wintypes.HANDLE,
    wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
    dll=kernel32)
_CloseHandle = _proto("CloseHandle", wintypes.BOOL, wintypes.HANDLE,
                      dll=kernel32)


def monitor_rect(hwnd):
    """(x, y, w, h) of the monitor this window is on (the nearest one if
    it is off-screen)."""
    hmon = _MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    _GetMonitorInfoW(hmon, ctypes.byref(mi))
    r = mi.rcMonitor
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def window_rect(hwnd):
    rc = wintypes.RECT()
    user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rc))
    return (rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top)


def borderless_fullscreen(hwnd):
    """Cover the monitor this window is on, borderless. Returns what
    unfullscreen() needs to put it back.

    Tk's own -fullscreen always lands on the primary monitor, and toggling
    wm overrideredirect makes Tk recreate the OS window - which would pull
    the drawable out from under libvlc. Swapping the frame style in place
    keeps every hwnd alive, and libvlc resizes its picture to match.
    """
    style = _GetWindowLongW(hwnd, GWL_STYLE)
    saved = (style, window_rect(hwnd))
    x, y, w, h = monitor_rect(hwnd)
    _SetWindowLongW(hwnd, GWL_STYLE, (style & ~WS_OVERLAPPEDWINDOW) | WS_POPUP)
    user32.SetWindowPos(wintypes.HWND(hwnd), None, x, y, w, h,
                        SWP_FRAMECHANGED | SWP_NOZORDER)
    return saved


def unfullscreen(hwnd, saved):
    """Undo borderless_fullscreen() with the state it handed back."""
    style, (x, y, w, h) = saved
    _SetWindowLongW(hwnd, GWL_STYLE, style)
    user32.SetWindowPos(wintypes.HWND(hwnd), None, x, y, w, h,
                        SWP_FRAMECHANGED | SWP_NOZORDER)


def placement(hwnd):
    """Where a window is, for put_placement() to put it back: its
    WINDOWPLACEMENT (normal, maximized or minimized, and the rectangle it
    restores to) and the rectangle it is at.

    The two rectangles differ for a window snapped to a side or a corner
    of a monitor (Win+Left/Right, or dragged to the edge): the placement's
    is where it was before the snap, and that is where Windows puts it
    when it is shown again normally, as Tk's deiconify shows it. On the
    PC, a film snapped to the right half of a monitor came back from the
    stream swap at the smaller size it had before, and stayed there.
    """
    wp = WINDOWPLACEMENT(length=ctypes.sizeof(WINDOWPLACEMENT))
    _GetWindowPlacement(hwnd, ctypes.byref(wp))
    return wp, window_rect(hwnd)


def put_placement(hwnd, saved):
    """Put a window back where placement() found it, without activating it.

    A snapped window comes back at its snapped rectangle, but as a window
    that is merely there - no call snaps one - so a drag no longer gives
    it its size from before the snap. A maximized one is maximized again
    on the monitor it was on.
    """
    wp, rect = saved
    hw = wintypes.HWND(hwnd)
    zoomed = wp.showCmd == SW_SHOWMAXIMIZED
    if user32.IsIconic(hw) or (user32.IsZoomed(hw)
                               and (not zoomed or window_rect(hwnd) != rect)):
        # Normal first: a placement set on a window still maximized does
        # not move it, measured in #26 (a browser maximized on one monitor
        # stayed on the other).
        user32.ShowWindow(hw, SW_SHOWNOACTIVATE)
    back = WINDOWPLACEMENT.from_buffer_copy(wp)
    back.showCmd = SW_SHOWMAXIMIZED if zoomed else SW_SHOWNOACTIVATE
    _SetWindowPlacement(hwnd, ctypes.byref(back))
    if not zoomed:
        x, y, w, h = rect
        user32.SetWindowPos(hw, None, x, y, w, h,
                            SWP_NOZORDER | SWP_NOACTIVATE)


def no_activate_on_click(hwnd):
    """Let clicks on this top-level window work without activating it.

    For controls floated over a fullscreen film: a click that made them
    the active window would take the keyboard (Space, Esc) away from the
    film and let the taskbar back over it. Tk answers Windows' "activate
    on click?" (WM_MOUSEACTIVATE) with yes for every window, and that
    answer beats the WS_EX_NOACTIVATE style - measured - so this window
    answers it itself. Returns the hook, which must be kept alive for as
    long as the window is.
    """
    prev = None

    def proc(h, msg, wp, lp):
        if msg == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE
        return _CallWindowProcW(prev, h, msg, wp, lp)

    hook = WNDPROC(proc)
    prev = _SetWindowLongPtrW(hwnd, GWLP_WNDPROC,
                              ctypes.cast(hook, ctypes.c_void_p))
    return hook


def foreground():
    """The top-level window that has the keyboard now (0 if none)."""
    return _GetForegroundWindow() or 0


def list_windows():
    """[(hwnd, title)] for visible, titled top-level windows."""
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            title = window_title(hwnd)
            if title:
                out.append((int(hwnd), title))
        return True

    user32.EnumWindows(cb, 0)
    return out


def window_title(hwnd):
    """A window's title; "" for one with none, or one that has gone. Never
    waits on the window's program: another process's title is read from
    what Windows keeps for it."""
    h = wintypes.HWND(hwnd)
    n = user32.GetWindowTextLengthW(h)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(h, buf, n + 1)
    return buf.value


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


def bring_forward(hwnd):
    """Un-minimize a window if it is minimized (left as it is otherwise:
    SW_RESTORE would also un-maximize it) and give it the keyboard. A
    process just started by the one in front - a second launch - may."""
    h = wintypes.HWND(hwnd)
    if user32.IsIconic(h):
        user32.ShowWindow(h, SW_RESTORE)
    user32.BringWindowToTop(h)
    user32.SetForegroundWindow(h)


def window_pid(hwnd):
    """The process id a window belongs to (0 if it is gone)."""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return pid.value


def window_class(hwnd):
    """A window's class name: "TkTopLevel" for a Tk toplevel's frame."""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wintypes.HWND(hwnd), buf, len(buf))
    return buf.value


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


def window_exe(hwnd):
    """The file name of the program a window belongs to, lower case
    ("chrome.exe"); "" if Windows won't say (an elevated process, a
    window gone)."""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    proc = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not proc:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not _QueryFullProcessImageNameW(proc, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        _CloseHandle(proc)


def find_stream_window(preferred_hwnd=None, title_hint=""):
    """The stream's browser window: the pinned pick if still alive, else the
    saved title, else anything that looks like Twitch/Kick, else the one
    browser window open."""
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
    # A live stream's tab is titled "... - Twitch", but a VOD's is named
    # after the VOD. A lone browser window is the stream; of several, any
    # pick is a guess, and a wrong one is raised over the film every pause.
    browsers = [hwnd for hwnd, _title in wins if window_exe(hwnd) in BROWSER_EXES]
    return browsers[0] if len(browsers) == 1 else None
