"""Checks the shell's update flow (ui/update.py) with a fake updater.

Nothing touches the network, and the first part needs no display: the
flow runs on a stub app whose Update dialog and Settings line only record
what they are told, and its worker "thread" runs where it is started, so
every check is deterministic. Covered:

- a check always answers once - update, update_none or update_err - and
  only one runs at a time; every answer reaches the Settings line;
- a newer release opens the offer once, and says when this copy cannot
  install it (Windows not run from the installer's folder);
- the download goes through updater's signed-checksums API with the
  release itself;
- Cancel removes the download folder and posts update_cancelled; a
  failure posts update_failed with the reason and removes it too;
- progress posts at most one event per percent (per MB with no size);
- update_ready closes the app through _on_close, once;
- the release-notes link only ever opens a GitHub page.

With a display, the real Update dialog is then driven through the same
flow: offer, progress, installing, failed and cancelled. Without one
that part prints SKIPPED.

    python test_updateflow.py
"""

import os
import queue
import shutil
import sys
import threading
import traceback
import types
from types import SimpleNamespace

if sys.platform != "win32" and "windowctl" not in sys.modules:
    # windowctl calls windll as it loads; only the Windows adapter's update
    # seam is used here, which never touches it
    sys.modules["windowctl"] = types.ModuleType("windowctl")

import ui.dialogs as real_dialogs            # noqa: E402
import ui.platform_win as platform_win       # noqa: E402
import ui.shell as shell                     # noqa: E402
import ui.update as update                   # noqa: E402
from version import __version__              # noqa: E402

fails = []


def check(label, got, want):
    if got != want:
        fails.append(f"{label}: got {got!r}, wanted {want!r}")


# --- fakes -------------------------------------------------------------------

class UpdateError(Exception):
    pass


MiB = 1 << 20
RELEASES_PAGE = "https://github.com/gBear97/StreamSync/releases/latest"


class FakeUpdater:
    """updater's API. exe is the installed StreamSync.exe that
    installed_exe_path names (None: a copy the installer did not put
    there)."""

    SUMS_ASSET = "SHA256SUMS"
    RELEASES_PAGE = RELEASES_PAGE
    UpdateError = UpdateError

    def __init__(self, exe=None):
        self.available = ("v9.9.9", {"tag_name": "v9.9.9"})
        self.downloads = []
        self.total = 10 * MiB
        self.chunk = 4096
        self.during = None           # called after the first progress call
        self.download_error = None
        self.verified_sums = lambda rel, key=None: ""
        self.installed_exe_path = lambda: exe

    def available_update(self):
        if isinstance(self.available, Exception):
            raise self.available
        return self.available

    def pick_asset(self, assets):
        for a in assets:
            if a.get("name", "").endswith(".dmg"):
                return a
        raise UpdateError("That release has no disk image for arm64.")

    def download_verified(self, asset, second, dest, progress=None):
        self.downloads.append((asset, second, dest))
        done = 0
        while not self.total or done < self.total:
            done = min(done + self.chunk, self.total) if self.total else done + self.chunk
            if progress:
                progress(done, self.total)
            if self.during:
                self.during()
                self.during = None
            if not self.total and done >= 3 * MiB:
                break
        if self.download_error:
            raise self.download_error
        path = os.path.join(dest, asset["name"])
        with open(path, "wb") as f:
            f.write(b"x")
        return path


class SyncThread:
    """Runs its target where start() is called, so the worker's posts are
    all in the queue when start() returns."""

    def __init__(self, target, daemon=None):
        self.target = target

    def start(self):
        self.target()


class FakeDialog:
    """An Update dialog that records what the flow tells it."""

    opened = []

    def __init__(self, app, tag, rel, can_install, unavailable_text):
        self.app, self.tag, self.rel = app, tag, rel
        self.can_install, self.unavailable_text = can_install, unavailable_text
        self.calls = []
        app._update_dialog = self
        FakeDialog.opened.append(self)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a: self.calls.append((name,) + a)

    def close(self):
        self.calls.append(("close",))
        if self.app._update_dialog is self:
            self.app._update_dialog = None


class FakeSettings:
    def __init__(self):
        self.lines = []

    def set_update_line(self, text, action):
        self.lines.append((text, action))


UPDATE_KINDS = {k: v for k, v in shell.Shell.HANDLERS.items() if k.startswith("update")}


class App(update.UpdateFlow):
    """Just what UpdateFlow touches: the queue, the dialog slots, the
    platform's install seam and _on_close."""

    UPDATE_UNAVAILABLE = "This copy can't update itself."

    def __init__(self, target="/Applications/StreamSync.app"):
        self.q = queue.Queue()
        self._settings = None
        self._update_dialog = None
        self.target = target
        self.installs = []
        self.install_error = None
        self.closes = 0
        self.events = []
        self._init_updates()

    def _installed_target(self):
        return self.target

    def _install_update(self, path, target, say):
        self.installs.append((path, target))
        say("Copying the new version into place...")
        if self.install_error:
            raise self.install_error

    def _on_close(self):
        self.closes += 1

    def drain(self):
        """The queue's events, without running them."""
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                return out

    def pump(self):
        """Run the queued events through the shell's own handler table."""
        for kind, *payload in self.drain():
            self.events.append((kind, *payload))
            getattr(self, UPDATE_KINDS[kind])(*payload)


class WinApp(App):
    """Windows' own seam (ui/platform_win's _installed_target): the
    installer's exe, from updater.installed_exe_path. It reads updater in
    its own module, so that gets the same fake."""

    def _installed_target(self):
        platform_win.updater = update.updater
        return platform_win.Adapter._installed_target(self)


opened_urls = []
real_updater, real_threading = update.updater, update.threading
real_webbrowser = update.webbrowser


def use(fake):
    update.updater = fake
    return fake


update.threading = SimpleNamespace(Event=threading.Event, Thread=SyncThread)
update.dialogs = SimpleNamespace(UpdateDialog=FakeDialog)
update.webbrowser = SimpleNamespace(open=lambda url: opened_urls.append(url))

DMG = {"name": "StreamSync-9.9.9-arm64-abc.dmg", "browser_download_url": "https://x/dmg",
       "size": 10 * MiB}
SUMS = {"name": "SHA256SUMS", "browser_download_url": "https://x/sums"}
REL = {"tag_name": "v9.9.9", "assets": [DMG, SUMS],
       "html_url": "https://github.com/gBear97/StreamSync/releases/tag/v9.9.9"}

try:
    # --- the handler table ------------------------------------------------
    for kind, name in UPDATE_KINDS.items():
        check(f"HANDLERS[{kind!r}] is UpdateFlow's", hasattr(update.UpdateFlow, name), True)
    check("every update kind is handled", sorted(UPDATE_KINDS), sorted(
        ["update", "update_none", "update_err", "update_progress", "update_step",
         "update_installing", "update_ready", "update_failed", "update_cancelled"]))

    # --- checking ----------------------------------------------------------
    up = use(FakeUpdater())
    a = App()
    a._settings = FakeSettings()
    check("before a check: the version", a._update_line, f"StreamSync {__version__}")
    a._check_updates(quiet=True)
    check("a check says it is checking", a._settings.lines[-1],
          ("Checking for updates…", None))
    a._check_updates(quiet=False)             # one is still unanswered
    events = a.drain()
    check("one check at a time", events, [("update", "v9.9.9", {"tag_name": "v9.9.9"})])
    for e in events:
        a.q.put(e)
    a.pump()                                  # the answer arrives
    check("quiet or not, a newer release opens the offer", len(FakeDialog.opened), 1)
    d = a._update_dialog
    check("the offer's version", (d.tag, d.can_install, d.unavailable_text),
          ("v9.9.9", True, App.UPDATE_UNAVAILABLE))
    check("the Settings line for a newer release", a._settings.lines[-1],
          ("StreamSync 9.9.9 is available.", "update"))
    check("the offer is kept", a._update_offer, ("v9.9.9", {"tag_name": "v9.9.9"}))
    a._check_updates(quiet=False)
    a.pump()
    check("an open offer is not opened twice", len(FakeDialog.opened), 1)
    a._show_update_offer()
    check("Update... lifts the open offer", d.calls[-1], ("lift",))
    d.close()
    a._show_update_offer()
    check("Update... reopens the offer the check found", len(FakeDialog.opened), 2)
    a._update_dialog.close()

    up.available = (None, None)
    a._check_updates()
    a.pump()
    check("no newer release", a.events[-1], ("update_none", __version__))
    check("the Settings line when up to date", a._settings.lines[-1],
          (f"StreamSync {__version__} is up to date.", "check"))
    check("no offer is kept", a._update_offer, None)
    a._show_update_offer()
    check("Update... with nothing found checks again", a.drain(),
          [("update_none", __version__)])
    a._update_checking = False

    for error, text in ((UpdateError("Could not reach GitHub: offline"),
                         "Could not reach GitHub: offline"),
                        (RuntimeError("something else"), "something else")):
        up.available = error
        a._check_updates()
        a.pump()
        check(f"a failed check posts update_err ({type(error).__name__})", a.events[-1],
              ("update_err", text))
        check("the Settings line when a check fails", a._settings.lines[-1],
              (f"Couldn't check for updates: {text}", "check"))
        check("a failed check can be retried", a._update_checking, False)

    # an offer arriving while an update runs opens nothing
    up.available = ("v9.9.9", REL)
    b = App()
    b._update_running = True
    n = len(FakeDialog.opened)
    b._check_updates()
    b.pump()
    check("no offer while an update runs", len(FakeDialog.opened), n)

    # --- what this copy can install -----------------------------------------
    use(FakeUpdater())
    w = WinApp()
    w._on_update("v9.9.9", REL)
    check("Windows not run from the installer's folder: the offer cannot install",
          (w._update_dialog.can_install, w._update_dialog.unavailable_text),
          (False, App.UPDATE_UNAVAILABLE))
    w._start_update("v9.9.9", REL)
    check("and Install does nothing", (w.drain(), w._update_running), ([], False))
    use(FakeUpdater(exe=r"C:\Programs\StreamSync\StreamSync.exe"))
    w = WinApp()
    w._on_update("v9.9.9", REL)
    check("Windows with installed_exe_path: the offer installs", w._update_dialog.can_install,
          True)
    m = App(target=None)
    m._on_update("v9.9.9", REL)
    check("a copy run from source cannot install", m._update_dialog.can_install, False)

    # --- the download ---------------------------------------------------------
    def run(fake, app=None, tag="v9.9.9", rel=REL):
        use(fake)
        app = app or App()
        app._on_update(tag, rel)
        app._start_update(tag, rel)
        return app

    up = FakeUpdater()
    a = run(up)
    asset, second, dest = up.downloads[0]
    check("the signed updater gets the release itself", (asset, second), (DMG, REL))
    check("the signed updater: the verified text", a._verified_text(),
          "Checked against the release's signed checksums. Installing...")

    # --- a whole update ----------------------------------------------------------
    events = a.drain()
    kinds = [e[0] for e in events]
    check("the order of a whole update", [k for k in kinds if k != "update_progress"],
          ["update_installing", "update_step", "update_step", "update_ready"])
    check("the install steps", [e[1] for e in events if e[0] == "update_step"],
          ["Checked against the release's signed checksums. Installing...",
           "Copying the new version into place..."])
    check("the platform installs the download into the target", a.installs,
          [(os.path.join(dest, DMG["name"]), "/Applications/StreamSync.app")])
    check("a finished download is kept for the installer", os.path.isdir(dest), True)
    pcts = [e[1] * 100 // e[2] for e in events if e[0] == "update_progress"]
    check("progress: one event per percent", (len(pcts), len(set(pcts)), pcts == sorted(pcts)),
          (101, 101, True))
    check("progress names the file", {e[3] for e in events if e[0] == "update_progress"},
          {DMG["name"]})
    for e in events:
        a.q.put(e)
    a.pump()
    calls = [c[0] for c in a._update_dialog.calls]
    check("the dialog is shown the download", calls[0], "show_progress")
    check("then the install", [c for c in calls if c != "show_progress"],
          ["set_installing", "show_step", "show_step"])
    check("update_ready closes the app once", a.closes, 1)
    shutil.rmtree(dest, ignore_errors=True)

    up = FakeUpdater()
    up.total = 0                              # a release asset with no size
    a = run(up)
    events = a.drain()
    mbs = [e[1] // MiB for e in events if e[0] == "update_progress"]
    check("no size: one event per MB", mbs, [0, 1, 2, 3])
    shutil.rmtree(up.downloads[0][2], ignore_errors=True)

    use(FakeUpdater())
    a = App()
    a._start_update("v9.9.9", REL)
    a._start_update("v9.9.9", REL)
    check("one update at a time", len(update.updater.downloads), 1)
    shutil.rmtree(update.updater.downloads[0][2], ignore_errors=True)

    # --- cancelling ------------------------------------------------------------
    up = FakeUpdater()
    a = App()
    up.during = a._cancel_update              # Cancel, mid-download
    run(up, a)
    events = a.drain()
    check("Cancel posts update_cancelled", events[-1], ("update_cancelled",))
    check("Cancel stops the download at once",
          sum(1 for e in events if e[0] == "update_progress"), 1)
    check("Cancel installs nothing", (a.installs, "update_installing" in [e[0] for e in events]),
          ([], False))
    check("Cancel removes the download folder", os.path.exists(up.downloads[0][2]), False)
    a.q.put(events[-1])
    a.pump()
    check("update_cancelled closes the dialog", (a._update_dialog, a._update_running),
          (None, False))

    up = FakeUpdater()
    up.total = 4096                           # one chunk: Cancel comes after the last one
    a = App()
    up.during = a._cancel_update
    run(up, a)
    check("Cancel after the last chunk still stops before installing",
          (a.drain()[-1], a.installs), (("update_cancelled",), []))
    check("and removes the download", os.path.exists(up.downloads[0][2]), False)

    a._on_update_cancelled()
    a._start_update("v9.9.9", REL)
    check("a new start clears the cancel", a._update_cancel.is_set(), False)
    shutil.rmtree(update.updater.downloads[-1][2], ignore_errors=True)
    a.drain()

    # --- failures ----------------------------------------------------------------
    for label, setup, why in (
            ("a bad checksum", lambda u, ap: setattr(u, "download_error", UpdateError(
                "Checksum mismatch for x.dmg")), "Checksum mismatch for x.dmg"),
            ("a failed install", lambda u, ap: setattr(ap, "install_error", UpdateError(
                "The downloaded app's signature is not intact")),
             "The downloaded app's signature is not intact"),
            ("no asset for this machine", None, "That release has no disk image for arm64.")):
        up = FakeUpdater()
        a = App()
        if setup:
            setup(up, a)
            run(up, a)
        else:
            run(up, a, rel={"tag_name": "v9.9.9", "assets": [SUMS]})
        events = a.drain()
        check(f"{label}: update_failed with the reason", events[-1], ("update_failed", why))
        if up.downloads:
            check(f"{label}: the download folder is removed",
                  os.path.exists(up.downloads[0][2]), False)
        a.q.put(events[-1])
        a.pump()
        check(f"{label}: the dialog shows why", a._update_dialog.calls[-1], ("show_failed", why))
        check(f"{label}: no longer running", a._update_running, False)
        check(f"{label}: the app stays open", a.closes, 0)

    a = App()
    a._update_offer = ("v9.9.9", REL)
    a._on_update_failed("gone")
    check("a failure with the dialog closed reopens it to say why",
          a._update_dialog.calls[-1], ("show_failed", "gone"))

    # --- the release pages -----------------------------------------------------
    use(FakeUpdater())
    a = App()
    a._open_release_notes(REL)
    a._open_release_notes({"html_url": "https://example.com/not-ours"})
    a._open_release_notes({"tag_name": "v9.9.9"})
    a._open_release_notes(None)
    a._open_releases_page()
    check("release notes open GitHub only", opened_urls,
          [REL["html_url"]] + [RELEASES_PAGE] * 4)
finally:
    update.threading = real_threading
    update.dialogs = real_dialogs
    update.webbrowser = real_webbrowser


# --- the real dialog, when there is a display ------------------------------------

def real_dialog():
    import tkinter as tk
    from ui import theme
    try:
        root = tk.Tk()
    except tk.TclError as e:
        print(f"SKIPPED real dialog: no display ({e})")
        return
    errors = []
    root.report_callback_exception = lambda *exc: errors.append(
        "".join(traceback.format_exception(*exc)))
    theme.init(1.0, "TkDefaultFont")
    update.threading = SimpleNamespace(Event=threading.Event, Thread=SyncThread)

    class TkApp(App):
        def __init__(self):
            super().__init__()
            self.root = root

        def _present(self, win):
            pass

        def _style_window(self, win):
            pass

    def settle():
        for _ in range(3):
            root.after(5)                     # a deferred click is due after 1 ms
            root.update()

    def pump():
        settle()
        a.pump()
        settle()

    try:
        up = use(FakeUpdater())
        up.download_error = UpdateError("Checksum mismatch for x.dmg")
        a = TkApp()
        a._on_update("v9.9.9", REL)
        d = a._update_dialog
        check("real: the offer", (type(d).__name__, d.view, d.win.title()),
              ("UpdateDialog", "offer", "StreamSync - Update"))
        d.install_btn.invoke()
        pump()
        check("real: a failed download shows why", d.view, "failed")
        check("real: and is no longer running", a._update_running, False)
        d._close_box()
        root.update()
        check("real: the close box closes a failed update", a._update_dialog, None)

        up = use(FakeUpdater())
        a = TkApp()
        a._on_update("v9.9.9", REL)
        d = a._update_dialog
        up.during = lambda: (d._close_box(), check("real: the close box cancels a download",
                                                   d.step_lbl.cget("text"), "Cancelling..."))
        d.install_btn.invoke()
        pump()
        check("real: a cancelled update closes its dialog", (a._update_dialog, d._closed),
              (None, True))

        up = use(FakeUpdater())
        a = TkApp()
        a._on_update("v9.9.9", REL)
        d = a._update_dialog
        d.install_btn.invoke()
        settle()
        events = a.drain()
        for e in events:
            if e[0] == "update_ready":
                break
            a.q.put(e)
        a.pump()
        root.update()
        check("real: installing", (d.view, d.cancel_btn.cget("state"),
                                   d.step_lbl.cget("text")),
              ("progress", "disabled", "Copying the new version into place..."))
        d._close_box()
        root.update()
        check("real: the close box is ignored while installing", a._update_dialog, d)
        a.q.put(("update_ready",))
        a.pump()
        check("real: update_ready closes the app", a.closes, 1)
        shutil.rmtree(up.downloads[0][2], ignore_errors=True)
        d.close()

        a = TkApp()
        a.target = None
        a._on_update("v9.9.9", REL)
        d = a._update_dialog
        check("real: the cannot-install offer", d.view, "unavailable")
        d.close()
        check("real: no Tk callback errors", errors, [])
    finally:
        update.threading = real_threading
        root.destroy()


real_dialog()
update.updater = platform_win.updater = real_updater

if fails:
    print("UPDATE FLOW TEST FAILED")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("UPDATE FLOW TEST PASSED")
