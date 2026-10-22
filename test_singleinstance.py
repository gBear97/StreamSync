"""One StreamSync at a time (singleinstance.py), with locks of the test's
own: every process here gets a home folder of its own and a
STREAMSYNC_INSTANCE name the user's StreamSync does not use, so this runs
beside it and writes nothing of theirs. On every OS:

- the first claim is StreamSync, and claiming again in the same process
  still is;
- while another process holds it, a claim is not, and asks that process
  (the one the lock file names) to come forward;
- once that process has gone, killed rather than quit, a claim is
  StreamSync again, and the lock file names it;
- `python streamsync.py` with one running exits at once, with 0, without
  starting the app; --diagnose and --selftest still run with one
  running - CI and the release workflow call them.

On Windows the holder shows a Tk window titled StreamSync, and a second
launch finds it by the lock file's process id. A holder with no window
yet is waited for: once it is gone the waiting launch is StreamSync, and
one that never shows a window is reported (in a message box, recorded
here), not waited on for ever.
"""

import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = tempfile.mkdtemp(prefix="streamsync-instance-")
NAME = f"StreamSyncTest{os.getpid()}"
os.environ["STREAMSYNC_INSTANCE"] = NAME
# expanduser reads HOME on the Mac and Linux, USERPROFILE on Windows
os.environ["HOME"] = os.environ["USERPROFILE"] = HOME

import singleinstance  # noqa: E402  (after the environment above)

WINDOWS = sys.platform == "win32"
HANDOFF = "StreamSync is already running"

HOLDER = """
import sys, time
import singleinstance
ok = singleinstance.claim()
win = None
if ok and "--window" in sys.argv:
    import tkinter as tk
    win = tk.Tk()
    win.title("StreamSync")
    win.geometry("240x90+60+60")
    win.update()
print("held" if ok else "refused", flush=True)
end = time.time() + 90
while time.time() < end:
    if win is not None:
        win.update()
    time.sleep(0.05)
"""

asked = []          # what the claims in this process asked for: ("pid"|"hwnd"|"tell", x)


def stub_hand_off():
    singleinstance._bring_forward_posix = lambda pid: asked.append(("pid", pid))
    singleinstance._bring_forward_windows = lambda hwnd: asked.append(("hwnd", hwnd))
    singleinstance._tell = lambda text: asked.append(("tell", text))


def hold(window=False):
    """Another process holding the lock: (Popen, its pid)."""
    proc = subprocess.Popen([sys.executable, "-c", HOLDER] + (["--window"] if window else []),
                            cwd=HERE, stdout=subprocess.PIPE, text=True)
    line = proc.stdout.readline().strip()
    assert line == "held", f"the holder said {line!r} (exit {proc.poll()})"
    return proc


def stop(proc):
    proc.kill()
    proc.wait(10)


def release():
    """Let go of this process's claim, as its exit would."""
    held, singleinstance._held = singleinstance._held, None
    if held is None:
        return
    if WINDOWS:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32")
        k32.ReleaseMutex(wintypes.HANDLE(held))
        k32.CloseHandle(wintypes.HANDLE(held))
    else:
        os.close(held)


def lock_pid():
    return singleinstance._read_pid()


def test_first_claim_is_streamsync():
    assert singleinstance.claim() is True
    assert singleinstance.claim() is True, "a second claim in one process failed"
    assert lock_pid() == os.getpid(), lock_pid()
    release()
    print("the first claim is StreamSync, and so is claiming again; the lock "
          "file names it")


def test_second_claim_hands_off():
    proc = hold(window=WINDOWS)
    try:
        asked.clear()
        assert singleinstance.claim() is False, "two claims held at once"
        assert singleinstance._held is None
        if WINDOWS:
            import windowctl
            assert [k for k, _ in asked] == ["hwnd"], asked
            hwnd = asked[0][1]
            assert windowctl.window_pid(hwnd) == proc.pid, "brought another window forward"
            assert windowctl.window_class(hwnd) == "TkTopLevel"
        else:
            assert asked == [("pid", proc.pid)], asked
    finally:
        stop(proc)
    asked.clear()
    assert singleinstance.claim() is True, "the lock outlived its holder"
    assert lock_pid() == os.getpid(), lock_pid()
    assert asked == [], asked
    release()
    print("while another holds it, a claim brings that one forward and is "
          "refused; once it is killed, a claim is StreamSync again")


def test_windows_waits_for_the_running_one():
    if not WINDOWS:
        return
    singleinstance.WAIT = 1.5
    proc = hold(window=False)
    try:
        asked.clear()
        t0 = time.monotonic()
        assert singleinstance.claim() is False
        took = time.monotonic() - t0
        assert [k for k, _ in asked] == ["tell"], asked
        assert 1.4 <= took < 6, took
    finally:
        stop(proc)
    singleinstance.WAIT = 10.0
    proc = hold(window=False)
    killer = threading.Timer(0.8, proc.kill)
    try:
        asked.clear()
        killer.start()
        t0 = time.monotonic()
        assert singleinstance.claim() is True, "a launch waiting on one that quit gave up"
        assert time.monotonic() - t0 < 6
        assert asked == [], asked
        assert lock_pid() == os.getpid(), lock_pid()
    finally:
        killer.cancel()
        stop(proc)
        release()
        singleinstance.WAIT = 5.0
    print("Windows: a running one with no window is told about after a wait; "
          "one that quits during it hands the lock over")


def launch(*args):
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "streamsync.py", *args], cwd=HERE,
                       capture_output=True, text=True, timeout=180)
    return r, time.monotonic() - t0


def test_streamsync_py_with_one_running():
    proc = hold(window=WINDOWS)
    try:
        r, took = launch()
        assert r.returncode == 0, (r.returncode, r.stderr[-2000:])
        assert HANDOFF in r.stderr, r.stderr[-2000:]
        assert " starting (" not in r.stderr, "the second launch went on to start"
        assert took < 30, took
        print(f"a second `streamsync.py` hands off and exits 0 ({took:.1f} s)")

        r, _ = launch("--diagnose")
        assert r.returncode == 0 and "StreamSync diagnostics" in r.stdout, \
            (r.returncode, r.stdout[-500:], r.stderr[-1000:])
        assert HANDOFF not in r.stderr
        print("--diagnose runs with one running")

        r, took = launch("--selftest")
        assert HANDOFF not in r.stderr, "--selftest handed off"
        # 0 is a whole tour (where VLC is installed); 2 or 3 is the gate
        # or the player saying VLC is missing - past the guard either way
        ran = (r.returncode == 0 and "SELFTEST OK" in r.stdout) or r.returncode in (2, 3)
        assert ran, (r.returncode, r.stdout[-500:], r.stderr[-2000:])
        print(f"--selftest runs with one running (exit {r.returncode}, {took:.1f} s)")
    finally:
        stop(proc)


def main():
    stub_hand_off()
    test_first_claim_is_streamsync()
    test_second_claim_hands_off()
    test_windows_waits_for_the_running_one()
    test_streamsync_py_with_one_running()
    print("SINGLE INSTANCE TEST PASSED")


if __name__ == "__main__":
    main()
