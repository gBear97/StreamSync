"""First-run dependency gate for StreamSync.

Checks - before the app itself loads - that VLC, the bundled ffmpeg and
the required Python packages are present, and offers one-click fixes:
pip for Python packages, winget (Windows' built-in, signature-verified
package manager) for VLC. Stdlib-only on purpose: it must run even when
none of the app's packages are installed yet.
"""

import importlib.util
import os
import queue
import shutil
import subprocess
import sys
import threading

# (import name, pip name)
REQUIRED_PKGS = [
    ("numpy", "numpy"),
    ("PIL", "Pillow"),
    ("mss", "mss"),
    ("imageio_ffmpeg", "imageio-ffmpeg"),
    ("vlc", "python-vlc"),
    ("soundcard", "soundcard"),
]
OPTIONAL_PKGS = [("keyboard", "keyboard")] if sys.platform == "win32" else []

IS_MAC = sys.platform == "darwin"
VLC64_DLL = r"C:\Program Files\VideoLAN\VLC\libvlc.dll"
VLC32_DLL = r"C:\Program Files (x86)\VideoLAN\VLC\libvlc.dll"
VLC_MAC_DYLIBS = [
    "/Applications/VLC.app/Contents/MacOS/lib/libvlc.dylib",
    os.path.expanduser("~/Applications/VLC.app/Contents/MacOS/lib/libvlc.dylib"),
]
BLACKHOLE_DRIVERS = [
    "/Library/Audio/Plug-Ins/HAL/BlackHole2ch.driver",
    "/Library/Audio/Plug-Ins/HAL/BlackHole16ch.driver",
]
# An .app launched from Finder inherits a minimal PATH (no ~/.zprofile), so
# Homebrew's bin is missing from it - look in its two standard locations.
BREW_PATHS = ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]
VLC_SITE = ("https://www.videolan.org/vlc/download-macosx.html" if IS_MAC
            else "https://www.videolan.org/vlc/download-windows.html")

if IS_MAC:
    UI_FONT, MONO_FONT = ("Helvetica Neue", 13, "bold"), ("Menlo", 11)
elif sys.platform == "win32":
    UI_FONT, MONO_FONT = ("Segoe UI", 10, "bold"), ("Consolas", 9)
else:
    UI_FONT, MONO_FONT = ("DejaVu Sans", 10, "bold"), ("DejaVu Sans Mono", 9)

_DEMO_NO_VLC = False  # test hook


def _noconsole():
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def frozen():
    return bool(getattr(sys, "frozen", False))


def missing_packages(pkgs=REQUIRED_PKGS):
    if frozen():
        return []
    return [pip for mod, pip in pkgs
            if importlib.util.find_spec(mod) is None]


def vlc_dll_path():
    """Path to a loadable libvlc, or None."""
    if _DEMO_NO_VLC:
        return None
    if IS_MAC:
        for p in VLC_MAC_DYLIBS:
            if os.path.isfile(p):
                return p
        return None
    if os.path.isfile(VLC64_DLL):
        return VLC64_DLL
    try:
        import winreg
        # 64-bit registry view only: a 32-bit VLC can't load into 64-bit Python
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\VideoLAN\VLC") as k:
            d, _ = winreg.QueryValueEx(k, "InstallDir")
        p = os.path.join(d, "libvlc.dll")
        if os.path.isfile(p):
            return p
    except (OSError, ImportError):  # ImportError: no winreg off Windows
        pass
    return None


def blackhole_present():
    """macOS only: is the BlackHole loopback driver installed? (None on Windows)"""
    if not IS_MAC:
        return None
    return any(os.path.isdir(p) for p in BLACKHOLE_DRIVERS)


def ffmpeg_ok():
    """True/False, or None if imageio-ffmpeg isn't installed yet."""
    if not frozen() and importlib.util.find_spec("imageio_ffmpeg") is None:
        return None
    try:
        import imageio_ffmpeg
        r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-version"],
                           capture_output=True, timeout=15,
                           creationflags=_noconsole())
        return r.returncode == 0
    except Exception:
        return False


def pkgmgr_path():
    """Path to Homebrew (macOS) / winget (Windows), or None.

    Returns a full path rather than a bare name: with Finder's minimal PATH
    neither the lookup nor a later subprocess call would find brew by name.
    """
    found = shutil.which("brew" if IS_MAC else "winget")
    if found or not IS_MAC:
        return found
    return next((p for p in BREW_PATHS if os.path.isfile(p)), None)


def collect():
    pkgs = missing_packages()
    return {
        "packages": pkgs,
        "optional": missing_packages(OPTIONAL_PKGS),
        "vlc": vlc_dll_path(),
        "vlc32_only": (not IS_MAC and vlc_dll_path() is None
                       and os.path.isfile(VLC32_DLL)),
        "ffmpeg": None if pkgs else ffmpeg_ok(),
        "pkgmgr": pkgmgr_path(),
        "blackhole": blackhole_present(),
    }


def all_ok(c):
    return not c["packages"] and c["vlc"] is not None and c["ffmpeg"] is True


def ensure_ready(force_dialog=False):
    """Return True when the app may start. Shows a fix-it dialog if needed."""
    # DPI awareness before any Tk window (process-wide, one-shot)
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    checks = collect()
    if all_ok(checks) and not force_dialog:
        return True
    return _Dialog(checks).run()


class _Dialog:
    def __init__(self, checks, demo=False):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.checks = checks
        self.demo = demo
        self.proceed = False
        self.q = queue.Queue()

        self.root = tk.Tk()
        self.root.title("StreamSync - first-run check")
        # Fixed-size everywhere except macOS, where leaving the window
        # resizable is the user's escape hatch if it ever comes up unpainted.
        self.root.resizable(IS_MAC, IS_MAC)
        frm = ttk.Frame(self.root, padding=12)
        frm.grid(sticky="nsew")

        ttk.Label(frm, text="StreamSync needs a couple of things before it "
                            "can start:", font=UI_FONT
                  ).grid(row=0, column=0, columnspan=2, sticky="w")

        self.row_lbls = {}
        for i, key in enumerate(("packages", "vlc", "ffmpeg", "blackhole",
                                 "optional"), start=1):
            lbl = ttk.Label(frm, text="")
            lbl.grid(row=i, column=0, columnspan=2, sticky="w", pady=(6, 0))
            self.row_lbls[key] = lbl

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.pip_btn = ttk.Button(btns, text="Install Python packages",
                                  command=self._fix_pip)
        self.pip_btn.pack(side="left")
        self.vlc_btn = ttk.Button(btns, text="Install VLC automatically",
                                  command=self._fix_vlc)
        self.vlc_btn.pack(side="left", padx=(8, 0))
        if IS_MAC:
            self.bh_btn = ttk.Button(btns, text="Install BlackHole",
                                     command=self._fix_blackhole)
            self.bh_btn.pack(side="left", padx=(8, 0))
            ttk.Button(btns, text="Open Audio MIDI Setup",
                       command=self._open_audio_midi_setup).pack(
                side="left", padx=(8, 0))
        else:
            self.bh_btn = None
        ttk.Button(btns, text="Open videolan.org instead",
                   command=self._open_vlc_site).pack(side="left", padx=(8, 0))

        self.out = tk.Text(frm, height=10, width=78, state="disabled",
                           font=MONO_FONT)
        self.out.grid(row=7, column=0, columnspan=2, pady=(10, 0))

        self.status = ttk.Label(frm, text="", foreground="#245")
        self.status.grid(row=8, column=0, columnspan=2, sticky="w", pady=(6, 0))

        bottom = ttk.Frame(frm)
        bottom.grid(row=9, column=0, columnspan=2, sticky="e", pady=(10, 0))
        self.cont_btn = ttk.Button(bottom, text="Start StreamSync",
                                   command=self._continue)
        self.cont_btn.pack(side="left")
        ttk.Button(bottom, text="Exit",
                   command=self.root.destroy).pack(side="left", padx=(8, 0))

        self._refresh()
        self.root.after(120, self._poll)

    # ---------------------------------------------------------------- checks

    def _refresh(self):
        c = self.checks
        if frozen():
            self.row_lbls["packages"].config(
                text="[OK] Python packages: bundled inside StreamSync.exe")
        elif c["packages"]:
            self.row_lbls["packages"].config(
                text="[MISSING] Python packages: " + ", ".join(c["packages"]))
        else:
            self.row_lbls["packages"].config(text="[OK] Python packages: all installed")

        if c["vlc"]:
            self.row_lbls["vlc"].config(text=f"[OK] VLC: {c['vlc']}")
        elif c["vlc32_only"]:
            self.row_lbls["vlc"].config(
                text="[MISSING] VLC: only 32-bit found - 64-bit VLC is required")
        else:
            self.row_lbls["vlc"].config(text="[MISSING] VLC media player")

        if c["blackhole"] is True:
            self.row_lbls["blackhole"].config(
                text="[OK] BlackHole audio driver: installed (route sound "
                     "through a Multi-Output Device)")
        elif c["blackhole"] is False:
            self.row_lbls["blackhole"].config(
                text="[NEEDED FOR AUDIO SYNC] BlackHole virtual audio driver "
                     "not found")
        else:
            self.row_lbls["blackhole"].config(text="")

        if c["ffmpeg"] is True:
            self.row_lbls["ffmpeg"].config(text="[OK] ffmpeg (bundled): working")
        elif c["ffmpeg"] is False:
            self.row_lbls["ffmpeg"].config(
                text="[PROBLEM] ffmpeg (bundled) failed to run - try reinstalling "
                     "the imageio-ffmpeg package")
        else:
            self.row_lbls["ffmpeg"].config(
                text="[  ] ffmpeg (bundled): checked after packages install")

        if c["optional"] and not frozen():
            self.row_lbls["optional"].config(
                text="[optional] " + ", ".join(c["optional"]) +
                     " - global hotkeys only; app works without it")
        else:
            self.row_lbls["optional"].config(text="")

        state_pip = "!disabled" if (c["packages"] and not frozen()) else "disabled"
        self.pip_btn.state([state_pip])
        self.vlc_btn.state(["!disabled"] if not c["vlc"] else ["disabled"])
        if not c["pkgmgr"]:
            self.vlc_btn.state(["disabled"])
        if self.bh_btn is not None:
            self.bh_btn.state(["!disabled"] if c["blackhole"] is False
                              else ["disabled"])
        self.cont_btn.state(["!disabled"] if all_ok(c) else ["disabled"])
        if all_ok(c):
            self.status.config(
                text="Everything is in place - you're good to go.")

    def _recheck(self):
        self.checks = collect()
        self._refresh()

    # ----------------------------------------------------------------- fixes

    def _fix_pip(self):
        pkgs = self.checks["packages"] + self.checks["optional"]
        self._run([sys.executable, "-m", "pip", "install",
                   "--disable-pip-version-check"] + pkgs,
                  "Python packages installed.")

    def _fix_vlc(self):
        mgr = self.checks["pkgmgr"]
        if not mgr:
            self._open_vlc_site()
            return
        if IS_MAC:
            self._log("Installing VLC through Homebrew (the cask downloads "
                      "VideoLAN's own signed app).\n")
            self._run([mgr, "install", "--cask", "vlc"],
                      "VLC installed successfully.")
        else:
            self._log("Installing VLC through winget (Microsoft's package "
                      "manager - the download is verified and the installer "
                      "is VideoLAN's own signed one). A Windows admin prompt "
                      "will appear; please approve it.\n")
            self._run([mgr, "install", "-e", "--id", "VideoLAN.VLC",
                       "--accept-source-agreements",
                       "--accept-package-agreements"],
                      "VLC installed successfully.")

    def _fix_blackhole(self):
        """One-click BlackHole install, no Homebrew needed.

        Downloads the developer's signed+notarized pkg from their official
        URL, but only after the file's sha256 matches the checksum that
        Homebrew publishes independently - two separate parties must agree
        on the bytes before macOS's own installer (which additionally
        verifies the signature and notarization) is asked to run it.
        """
        self.pip_btn.state(["disabled"])
        self.vlc_btn.state(["disabled"])
        if self.bh_btn is not None:
            self.bh_btn.state(["disabled"])
        self.status.config(text="Working...")

        def work():
            import hashlib
            import json
            import tempfile
            import urllib.request

            def get(url):
                req = urllib.request.Request(
                    url, headers={"User-Agent": "StreamSync-setup"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.read()

            try:
                self.q.put(("line", "Fetching current BlackHole version info "
                                    "(from Homebrew's public metadata)..."))
                meta = json.loads(get(
                    "https://formulae.brew.sh/api/cask/blackhole-2ch.json"))
                ver, url, sha = meta["version"], meta["url"], meta["sha256"]
                self.q.put(("line", f"BlackHole 2ch v{ver}: {url}"))

                self.q.put(("line", "Downloading installer..."))
                data = get(url)
                digest = hashlib.sha256(data).hexdigest()
                if digest != sha:
                    self.q.put(("done", 1,
                                "Checksum mismatch - refusing to install. "
                                "Use the videolan-style manual route: "
                                "https://existential.audio/blackhole/"))
                    return
                self.q.put(("line", f"Downloaded {len(data) // 1024} KB, "
                                    "sha256 verified against Homebrew's "
                                    "published checksum."))

                pkg = os.path.join(tempfile.mkdtemp(), f"BlackHole2ch-{ver}.pkg")
                with open(pkg, "wb") as f:
                    f.write(data)

                self.q.put(("line", "Running the macOS installer - enter your "
                                    "password in the system prompt. (macOS "
                                    "also verifies the developer's signature "
                                    "and notarization here.)"))
                r = subprocess.run(
                    ["osascript", "-e",
                     f'do shell script "installer -pkg \'{pkg}\' -target /" '
                     "with administrator privileges"],
                    capture_output=True, text=True, timeout=300)
                if r.returncode != 0:
                    err = (r.stderr or "").strip()
                    if "-128" in err:
                        self.q.put(("done", 1, "Install cancelled."))
                    else:
                        self.q.put(("done", 1, f"Installer failed: {err}"))
                    return
                self.q.put(("done", 0,
                            "BlackHole installed. Last step (one time): in "
                            "Audio MIDI Setup, click '+' > Create "
                            "Multi-Output Device, tick your speakers AND "
                            "BlackHole 2ch, then set it as the sound output."))
            except Exception as e:
                self.q.put(("done", 1, f"Download failed ({e}) - get it from "
                                       "https://existential.audio/blackhole/ "
                                       "instead."))

        threading.Thread(target=work, daemon=True).start()

    def _open_audio_midi_setup(self):
        try:
            subprocess.run(["open", "-a", "Audio MIDI Setup"], timeout=10)
        except Exception:
            pass

    def _open_vlc_site(self):
        import webbrowser
        webbrowser.open(VLC_SITE)
        self.status.config(
            text="Drag VLC into /Applications, then this dialog will re-check "
                 "automatically." if IS_MAC else
                 "Get the 64-bit Windows installer, run it, then this dialog "
                 "will re-check automatically.")
        self.root.after(4000, self._auto_recheck)

    def _auto_recheck(self):
        self._recheck()
        if not all_ok(self.checks):
            self.root.after(4000, self._auto_recheck)

    def _run(self, cmd, done_msg):
        if self.demo:
            self._log("(demo mode - not running: " + " ".join(cmd) + ")\n")
            return
        self.pip_btn.state(["disabled"])
        self.vlc_btn.state(["disabled"])
        self.status.config(text="Working...")

        def work():
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, errors="replace", creationflags=_noconsole())
                for line in proc.stdout:
                    line = line.strip("\r\n")
                    if line.strip():
                        self.q.put(("line", line))
                proc.wait()
                self.q.put(("done", proc.returncode, done_msg))
            except Exception as e:
                self.q.put(("done", -1, f"Failed to run: {e}"))

        threading.Thread(target=work, daemon=True).start()

    # -------------------------------------------------------------- plumbing

    def _poll(self):
        try:
            while True:
                kind, *payload = self.q.get_nowait()
                if kind == "line":
                    self._log(payload[0] + "\n")
                elif kind == "done":
                    rc, msg = payload
                    self._recheck()
                    if rc == 0:
                        self.status.config(text=msg + (
                            "  All checks pass - hit Start."
                            if all_ok(self.checks) else ""))
                    else:
                        self.status.config(
                            text=f"That didn't finish cleanly (exit {rc}) - "
                                 "see the log above, or use the manual "
                                 "download button.")
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _log(self, text):
        self.out.config(state="normal")
        self.out.insert("end", text)
        self.out.see("end")
        self.out.config(state="disabled")

    def _continue(self):
        self.proceed = True
        self.root.destroy()

    def _present(self):
        """Force the window to draw itself before we block in mainloop.

        A packaged .app starts out behind whatever the user was looking at
        and without keyboard focus; on macOS that can leave Tk showing a
        titled but completely empty window, since the contents are never
        painted. Claiming focus and nudging the geometry once makes Aqua
        lay it out for real.
        """
        r = self.root
        r.update_idletasks()
        if IS_MAC:
            w, h = r.winfo_reqwidth(), r.winfo_reqheight()
            r.geometry(f"{w}x{h + 1}")
            r.update_idletasks()
            r.geometry(f"{w}x{h}")
            r.lift()
            r.attributes("-topmost", True)
            r.after_idle(r.attributes, "-topmost", False)
            try:
                r.focus_force()
            except self.tk.TclError:
                pass
        r.update()

    def run(self):
        self._present()
        self.root.mainloop()
        return self.proceed


if __name__ == "__main__":
    # UI smoke test: pretend VLC is missing, auto-close, report
    if "--demo" in sys.argv:
        _DEMO_NO_VLC = True
        dlg = _Dialog(collect(), demo=True)
        dlg.root.after(2500, dlg.root.destroy)
        dlg.run()
        print("DEPCHECK UI OK")
    else:
        c = collect()
        print("packages missing:", c["packages"] or "none")
        print("optional missing:", c["optional"] or "none")
        print("vlc:", c["vlc"] or "NOT FOUND")
        print("ffmpeg ok:", c["ffmpeg"])
        print("package manager:", c["pkgmgr"] or "NOT FOUND")
        print("ALL OK" if all_ok(c) else "PROBLEMS FOUND")
