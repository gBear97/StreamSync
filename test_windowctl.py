"""Live check of windowctl: spawn a real window, find, minimize, restore."""

import subprocess
import sys
import time

TITLE = "WinCtlTest-9271"


def main():
    if sys.platform != "win32":
        print(f"SKIPPED: windowctl is Windows-only, this is {sys.platform} "
              "(use test_macwindowctl.py on macOS)")
        return
    import windowctl  # touches ctypes.windll, so only after the guard

    proc = subprocess.Popen([
        sys.executable, "-c",
        f"import tkinter as tk; r = tk.Tk(); r.title('{TITLE}'); "
        "r.geometry('200x100+50+50'); r.mainloop()",
    ])
    try:
        hwnd = None
        deadline = time.time() + 10
        while time.time() < deadline and hwnd is None:
            for h, t in windowctl.list_windows():
                if t == TITLE:
                    hwnd = h
                    break
            time.sleep(0.2)
        assert hwnd, "spawned window never appeared in list_windows()"
        assert windowctl.is_valid(hwnd)
        assert windowctl.find_by_pid(proc.pid) == hwnd

        windowctl.minimize(hwnd)
        time.sleep(0.4)
        assert windowctl.is_minimized(hwnd), "window did not minimize"
        windowctl.restore(hwnd)
        time.sleep(0.4)
        assert not windowctl.is_minimized(hwnd), "window did not restore"
        print("hwnd found by title and pid; minimize + restore both verified")
        print("WINDOWCTL TEST PASSED")
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
