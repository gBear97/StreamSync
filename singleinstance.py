"""One StreamSync at a time.

A second launch brings the one already running forward and exits. Two
instances each had a film window, a player and settings saves of their
own; on the PC, a tool that only meant to bring the app forward started
a second one. streamsync.py asks before anything else starts, and only
for the app itself: --netcheck and --diagnose have returned by then, and
--selftest (CI's smoke test, with a throwaway config) never asks, so all
three run while an instance is up.

Windows: a named mutex, owned by the running instance's main thread, so
Windows lets go of it when the process ends, however it ends. The Mac
(and any POSIX system): an exclusive flock on the lock file, which the
kernel lets go of the same way. On both, the lock file holds the running
instance's process id, which is how a second launch finds it.

On the Mac, Finder, the Dock and `open` never start a second instance of
an .app: LaunchServices brings the running one forward. This is for
`open -n`, the bundle's binary run directly, and a source checkout.

STREAMSYNC_INSTANCE names the lock (default "StreamSync"), so tests can
run instances of their own beside the user's.
"""

import os
import sys
import time

NAME = os.environ.get("STREAMSYNC_INSTANCE") or "StreamSync"
MUTEX = f"Local\\{NAME}-one-instance"      # Local: this logon session's
LOCK_PATH = os.path.join(os.path.expanduser("~"), f".{NAME.lower()}.lock")
# Windows: how long a second launch looks for the running one's window,
# or waits for it to finish quitting, before saying it can't be found
WAIT = 5.0
# the running instance's windows a second launch may bring forward: the
# main window, or the dependency gate's while it is still up
TITLES = ("StreamSync", "StreamSync - first-run check")

_held = None       # the mutex handle or the locked file, kept for the process's life


def claim():
    """True if this is the one StreamSync and should carry on. False if
    another is running: it has been asked to come forward, and this one
    should exit. Calling it again in the same process is True."""
    global _held
    if _held is not None:
        return True
    try:
        if sys.platform == "win32":
            return _claim_windows()
        return _claim_posix()
    except Exception as e:
        # a guard that cannot work must not stop the app from starting
        _log(f"single-instance check failed, starting anyway: {e!r}")
        return True


def _write_pid(fd):
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()}\n".encode())


def _read_pid():
    try:
        with open(LOCK_PATH, encoding="ascii") as f:
            return int(f.read().strip() or 0) or None
    except (OSError, ValueError):
        return None


# --- POSIX ---------------------------------------------------------------

def _claim_posix():
    global _held
    import fcntl
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        pid = _read_pid()
        _log(f"StreamSync is already running (pid {pid}): bringing it "
             "forward instead of starting another")
        _bring_forward_posix(pid)
        return False
    except OSError:
        os.close(fd)             # a filesystem without locks: no guard
        return True
    _write_pid(fd)
    _held = fd
    return True


def _bring_forward_posix(pid):
    # The running one has no window to be found from here, so System
    # Events brings its process forward, as the app's own swap does
    # (macwindowctl). Best effort: refused Automation, it stays behind.
    if sys.platform != "darwin" or not pid:
        return
    try:
        import macwindowctl
        macwindowctl.activate_pid(pid)
    except Exception as e:
        _log(f"could not bring the running StreamSync forward: {e}")


# --- Windows ---------------------------------------------------------------

ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0, WAIT_ABANDONED = 0, 0x80


def _kernel32():
    # Private, typed handles: windll.kernel32 is one object for the whole
    # process, so setting argtypes on it would change it for everyone.
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def _claim_windows():
    global _held
    import ctypes
    k32 = _kernel32()
    h = k32.CreateMutexW(None, True, MUTEX)
    if not h:
        return True              # cannot tell: better two instances than none
    if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
        _held = h                # created and owned: this is StreamSync
        _note_pid()
        return True
    deadline = time.monotonic() + WAIT
    while True:
        hwnd = _running_window(_read_pid())
        if hwnd:
            _log(f"StreamSync is already running: bringing its window "
                 f"{hwnd:#x} forward instead of starting another")
            _bring_forward_windows(hwnd)
            k32.CloseHandle(h)
            return False
        # it may be quitting: once it has, the mutex is this one's
        if k32.WaitForSingleObject(h, 200) in (WAIT_OBJECT_0, WAIT_ABANDONED):
            _held = h
            _note_pid()
            return True
        if time.monotonic() > deadline:
            k32.CloseHandle(h)
            _log("StreamSync is already running, but no window of it came up")
            _tell("StreamSync is already running, but its window did not come "
                  "up. If it does not appear, end StreamSync in Task Manager, "
                  "then start it again.")
            return False


def _note_pid():
    # The mutex is the lock; the file only says whose it is, so a second
    # launch brings forward that process's window and no other StreamSync
    # (a selftest's, say).
    try:
        fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _write_pid(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _running_window(pid):
    """The running instance's main window (or its dependency gate): a Tk
    toplevel with one of TITLES, of process `pid` - or, with no process
    id to go by, of any process but this one."""
    import windowctl
    mine = os.getpid()
    for hwnd, title in windowctl.list_windows():
        if title in TITLES and windowctl.window_class(hwnd) == "TkTopLevel":
            owner = windowctl.window_pid(hwnd)
            if owner != mine and (pid is None or owner == pid):
                return hwnd
    return None


def _bring_forward_windows(hwnd):
    import windowctl
    windowctl.bring_forward(hwnd)


def _tell(text):
    import ctypes
    MB_ICONINFORMATION, MB_SETFOREGROUND = 0x40, 0x10000
    ctypes.windll.user32.MessageBoxW(None, text, "StreamSync",
                                     MB_ICONINFORMATION | MB_SETFOREGROUND)


def _log(message):
    try:
        import diagnostics
        diagnostics.log(message)
    except Exception:
        pass
