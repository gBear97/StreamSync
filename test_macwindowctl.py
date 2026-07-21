"""Live check of macwindowctl: spawn a real GUI app, find, hide, reactivate.

The helper is this interpreter copied into a throwaway .app bundle so
System Events sees a unique app name — framework Python re-execs the
Python.app binary inside the framework, so anything spawned from
sys.executable registers as "Python" and would collide with any other
Tk app the user has open. System Events queries need Automation
permission (one-time macOS prompt); if denied, this aborts with a hint.
"""

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time

APP = "MacWinCtlTest-4407"
HERE = os.path.dirname(os.path.abspath(__file__))

HELPER = """
import sys, threading
import tkinter as tk
import macwindowctl

def listen():
    for line in sys.stdin:
        if line.strip() == "activate":
            macwindowctl.activate_self()

threading.Thread(target=listen, daemon=True).start()
r = tk.Tk()
r.title("MacWinCtlTest helper")
r.geometry("200x100+50+50")
r.mainloop()
"""


def check_permission(macwindowctl):
    try:
        # 30 s leaves time to answer the one-time Automation prompt.
        macwindowctl._osascript(
            'tell application "System Events" to get name of '
            "first application process", timeout=30.0)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        sys.exit(
            "FAILED: cannot query System Events — Automation permission "
            "denied or the macOS prompt went unanswered. Grant access in "
            "System Settings > Privacy & Security > Automation (allow your "
            f"terminal to control System Events), then re-run.\n({e})")


def make_helper_app(tmp):
    # ps, not sys.executable: framework builds re-exec the framework's
    # Python.app binary, and a copy of the outer stub would still
    # register with System Events under that bundle's name ("Python").
    exe = subprocess.run(["ps", "-p", str(os.getpid()), "-o", "comm="],
                         capture_output=True, text=True).stdout.strip()
    if not os.path.isabs(exe):
        exe = os.path.realpath(sys.executable)
    macos_dir = os.path.join(tmp, APP + ".app", "Contents", "MacOS")
    os.makedirs(macos_dir)
    helper = os.path.join(macos_dir, APP)
    shutil.copy2(exe, helper)
    plist = os.path.join(tmp, APP + ".app", "Contents", "Info.plist")
    with open(plist, "wb") as f:
        plistlib.dump({"CFBundleName": APP, "CFBundleExecutable": APP,
                       "CFBundleIdentifier": "com.streamsync.test.macwinctl",
                       "CFBundlePackageType": "APPL"}, f)
    return helper


def wait_for(what, cond, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.2)
    raise AssertionError(what)


def main():
    if sys.platform != "darwin":
        print(f"SKIPPED: macwindowctl is macOS-only, this is {sys.platform} "
              "(use test_windowctl.py on Windows)")
        return
    import macwindowctl

    def prop(pid, name):
        return macwindowctl._osascript(
            f'tell application "System Events" to get {name} of '
            f"(first application process whose unix id is {pid})")

    def registered(pid):
        try:
            return prop(pid, "name") == APP
        except RuntimeError:
            return False

    check_permission(macwindowctl)
    tmp = tempfile.mkdtemp(prefix="macwinctl-test-")
    proc = None
    try:
        helper = make_helper_app(tmp)
        # The copied binary lives outside the framework, so getpath needs
        # the venv launcher landmark to locate the stdlib.
        env = dict(os.environ, __PYVENV_LAUNCHER__=sys.executable)
        proc = subprocess.Popen([helper, "-c", HELPER], env=env, cwd=HERE,
                                stdin=subprocess.PIPE)
        wait_for("helper never registered with System Events",
                 lambda: registered(proc.pid), timeout=15)
        assert APP in macwindowctl.list_gui_apps(), \
            "helper missing from list_gui_apps()"
        # A hide sent while the app is still activating gets cancelled by
        # the launch activation, so wait until the window has taken focus.
        wait_for("helper window never took focus after launch",
                 lambda: prop(proc.pid, "frontmost") == "true", timeout=15)

        macwindowctl.hide_app(APP)
        wait_for("helper did not hide",
                 lambda: prop(proc.pid, "visible") == "false")
        macwindowctl.activate_app(APP)
        wait_for("helper did not reappear",
                 lambda: prop(proc.pid, "visible") == "true")
        wait_for("helper did not come to front",
                 lambda: prop(proc.pid, "frontmost") == "true")

        macwindowctl.hide_app(APP)
        wait_for("helper did not hide (second time)",
                 lambda: prop(proc.pid, "visible") == "false")
        proc.stdin.write(b"activate\n")
        proc.stdin.flush()
        wait_for("activate_self did not unhide the helper",
                 lambda: prop(proc.pid, "visible") == "true")
        wait_for("activate_self did not bring the helper to front",
                 lambda: prop(proc.pid, "frontmost") == "true")

        print("helper found in list_gui_apps; hide_app, activate_app "
              "and activate_self all verified")
        print("MACWINDOWCTL TEST PASSED")
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=5)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
