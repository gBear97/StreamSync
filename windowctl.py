"""Find, minimize and restore other applications' windows (Win32), and
put our own video window fullscreen on the right monitor.

Used to bring the stream's browser window into view while the streamer
has the film paused, then tuck it away again when the film resumes.
"""

import ctypes
import os
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

SW_MINIMIZE = 6
SW_RESTORE = 9

GWL_STYLE = -16
GWLP_WNDPROC = -4
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_POPUP = 0x80000000
SWP_NOZORDER = 0x0004
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
