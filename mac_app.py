"""StreamSync for macOS.

Same engine as the Windows app (audio-first sync against a live stream),
with a Mac-shaped shell: a small, minimal main window - film, Sync,
Resync, nudges, status - and every less-used option living in the native
menu bar (Sync, Playback, Advanced menus).

Platform notes:
- Audio capture comes from the BlackHole virtual device (no loopback API
  on macOS); audio_capture handles the routing details.
- The "embedded" player is a libvlc-owned video window (Tk cannot host
  libvlc on macOS); control is identical, VLC just draws its own window.
- The facecam swap activates/hides the browser app via AppleScript.
"""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageTk

import audio_capture
import audio_matcher
import capture
import depcheck
import macwindowctl
import matcher
import session
import updater
from version import __version__
import diagnostics
from players import EmbeddedPlayer, ExternalPlayer, VLCError

CONFIG_PATH = Path.home() / ".streamsync.json"
BURST_FRAMES = 4
BURST_SPACING = 1 / 3
AUDIO_SYNC_SECONDS = 6.0
AUTO_RECORD_SECONDS = 4.0
LOW_CONFIDENCE = 0.55
BROWSERS = ("Safari", "Google Chrome", "Firefox", "Arc", "Brave Browser",
            "Microsoft Edge", "Opera", "Vivaldi")


def fmt_time(s):
    s = max(0.0, float(s))
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    frac = s - int(s)
    if h:
        return f"{h}:{m:02d}:{sec + frac:04.1f}"
    return f"{m}:{sec + frac:04.1f}"


def parse_time(text):
    text = text.strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3 or not all(p.strip() for p in parts):
        raise ValueError(f"Cannot parse time '{text}' (use h:mm:ss, m:ss or seconds)")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


class MacApp:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.region = None
        self.video_path = None
        self.busy = False
        self.offset = 0.0
        self.fullscreen = False
        self.facecam_rect = None
        self.audio_device = ""
        self.stream_app = ""
        self.auto_enabled = False
        self.auto_follow = True
        self.auto_interval = 30
        self._closing = False
        self._swapped = False
        self._was_fullscreen = False
        self._preview_photo = None
        self.external = None
        self.session = None          # active HostSession / ViewerSession
        self.relay_url = "ws://localhost:8765"

        root.title(f"StreamSync {__version__}")
        root.resizable(False, False)

        # menu variables (created before menus reference them)
        self.method_var = tk.StringVar(value="audio")
        self.player_var = tk.StringVar(value="embedded")
        self.mute_var = tk.BooleanVar(value=True)
        self.auto_var = tk.BooleanVar(value=False)
        self.follow_var = tk.BooleanVar(value=True)
        self.interval_var = tk.IntVar(value=30)
        self.facecam_var = tk.StringVar(value="none")
        self.mirror_var = tk.BooleanVar(value=False)
        self.swap_var = tk.BooleanVar(value=True)
        self.device_var = tk.StringVar(value="")
        self.streamapp_var = tk.StringVar(value="")
        self.sub_var = tk.StringVar(value="")

        self._build_main()
        self._build_preview_window()

        try:
            self.player_backend = EmbeddedPlayer(None)  # libvlc's own window
        except VLCError as e:
            if "--selftest" in sys.argv:
                # Nobody can dismiss a modal in an unattended run, and a
                # blocked dialog looks identical to a hung app.
                print(f"SELFTEST: VLC unavailable - {e}", file=sys.stderr)
                raise SystemExit(2)
            # The Advanced menu is built further down, so it does not
            # exist yet - this dialog is the entire user interface in
            # this failure, and has to carry the diagnosis itself.
            diagnostics.log(f"player failed to start: {e}")
            messagebox.showerror("StreamSync - VLC problem", str(e))
            raise SystemExit(1)
        self.active_player = self.player_backend
        self._build_video_window()

        self._load_config()
        self._build_menus()
        self._populate_audio_devices()

        threading.Thread(target=self._auto_loop, daemon=True).start()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(80, self._poll_queue)
        root.after(700, self._tick_time)
        # Quiet: only speaks up when there is actually something newer.
        root.after(2500, lambda: self._check_updates(quiet=True))

    # -------------------------------------------------------------- main UI

    def _build_main(self):
        frm = ttk.Frame(self.root, padding=(18, 14, 18, 12))
        frm.grid(sticky="nsew")

        self.file_lbl = ttk.Label(frm, text="No film loaded  -  ⌘O",
                                  font=("SF Pro Text", 13))
        self.file_lbl.grid(row=0, column=0, columnspan=6, sticky="w")

        row = ttk.Frame(frm)
        row.grid(row=1, column=0, columnspan=6, sticky="w", pady=(10, 0))
        ttk.Label(row, text="around").pack(side="left")
        self.hint_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.hint_var, width=9).pack(
            side="left", padx=(6, 0))
        ttk.Label(row, text="±").pack(side="left", padx=(8, 0))
        self.window_var = tk.StringVar(value="2:00")
        ttk.Entry(row, textvariable=self.window_var, width=6).pack(
            side="left", padx=(6, 0))

        row = ttk.Frame(frm)
        row.grid(row=2, column=0, columnspan=6, sticky="w", pady=(12, 0))
        self.sync_btn = ttk.Button(row, text="Sync", width=12,
                                   command=self._sync)
        self.sync_btn.pack(side="left")
        self.resync_btn = ttk.Button(row, text="Resync", width=12,
                                     command=self._resync)
        self.resync_btn.pack(side="left", padx=(8, 0))

        row = ttk.Frame(frm)
        row.grid(row=3, column=0, columnspan=6, sticky="w", pady=(12, 0))
        for d in (-2.0, -0.5, -0.1, 0.1, 0.5, 2.0):
            ttk.Button(row, text=f"{d:+g}", width=5,
                       command=lambda d=d: self._nudge(d)).pack(side="left",
                                                                padx=1)
        self.offset_lbl = ttk.Label(row, text="  0.00s", foreground="#888")
        self.offset_lbl.pack(side="left", padx=(8, 0))

        self.time_lbl = ttk.Label(frm, text="", foreground="#888",
                                  font=("SF Pro Text", 11))
        self.time_lbl.grid(row=4, column=0, columnspan=6, sticky="w",
                           pady=(12, 0))
        self.status_lbl = ttk.Label(frm, text="Open a film to begin.",
                                    foreground="#666", wraplength=380,
                                    font=("SF Pro Text", 11))
        self.status_lbl.grid(row=5, column=0, columnspan=6, sticky="w",
                             pady=(2, 0))

    def _build_preview_window(self):
        self.preview_win = tk.Toplevel(self.root)
        self.preview_win.title("Capture preview")
        self.preview_lbl = ttk.Label(self.preview_win,
                                     text="No capture yet - preview appears "
                                          "after a video-method sync.")
        self.preview_lbl.pack(padx=12, pady=12)
        self.preview_win.protocol("WM_DELETE_WINDOW", self.preview_win.withdraw)
        self.preview_win.withdraw()

    # --------------------------------------------------------------- menus

    def _build_menus(self):
        m = tk.Menu(self.root)

        filem = tk.Menu(m, tearoff=0)
        filem.add_command(label="Open Film...", accelerator="Command-O",
                          command=self._choose_file)
        filem.add_command(label="Load Subtitle File...",
                          command=self._load_sub_file)
        m.add_cascade(label="File", menu=filem)

        syncm = tk.Menu(m, tearoff=0)
        syncm.add_command(label="Sync Now", accelerator="Command-S",
                          command=self._sync)
        syncm.add_command(label="Resync", accelerator="Command-R",
                          command=self._resync)
        syncm.add_separator()
        syncm.add_radiobutton(label="Sync by Audio", variable=self.method_var,
                              value="audio")
        syncm.add_radiobutton(label="Sync by Video Capture (Experimental)",
                              variable=self.method_var, value="video")
        syncm.add_separator()
        syncm.add_checkbutton(label="Auto Re-sync", variable=self.auto_var,
                              command=self._on_auto_toggle)
        syncm.add_checkbutton(label="Follow Stream Pauses",
                              variable=self.follow_var,
                              command=self._on_auto_toggle)
        ivm = tk.Menu(syncm, tearoff=0)
        for s in (15, 30, 45, 60, 120):
            ivm.add_radiobutton(label=f"Every {s} s",
                                variable=self.interval_var, value=s,
                                command=self._on_auto_toggle)
        syncm.add_cascade(label="Check Interval", menu=ivm)
        m.add_cascade(label="Sync", menu=syncm)

        self.session_menu = tk.Menu(m, tearoff=0)
        self.session_menu.add_command(label="Host a Session...",
                                      command=self._host_dialog)
        self.session_menu.add_command(label="Join a Session...",
                                      command=self._join_dialog)
        self.session_menu.add_separator()
        self.session_menu.add_command(label="Leave Session",
                                      command=self._leave_session,
                                      state="disabled")
        m.add_cascade(label="Session", menu=self.session_menu)

        playm = tk.Menu(m, tearoff=0)
        playm.add_command(label="Play / Pause", accelerator="Command-P",
                          command=self._toggle_pause)
        playm.add_command(label="Toggle Fullscreen",
                          accelerator="Shift-Command-F",
                          command=self._toggle_fullscreen)
        playm.add_command(label="Show Video Window",
                          command=self._show_video_window)
        playm.add_checkbutton(label="Mute Local Audio", variable=self.mute_var,
                              command=self._on_mute_toggle)
        playm.add_separator()
        playm.add_radiobutton(label="Player: Built-in VLC Window",
                              variable=self.player_var, value="embedded",
                              command=self._apply_player_choice)
        playm.add_radiobutton(label="Player: External VLC App",
                              variable=self.player_var, value="external",
                              command=self._apply_player_choice)
        playm.add_separator()
        self.subs_menu = tk.Menu(playm, tearoff=0)
        self._rebuild_subs_menu([])
        playm.add_cascade(label="Subtitle Track", menu=self.subs_menu)
        m.add_cascade(label="Playback", menu=playm)

        advm = tk.Menu(m, tearoff=0)
        advm.add_command(label="Select Capture Region...",
                         command=self._select_region)
        fcm = tk.Menu(advm, tearoff=0)
        fcm.add_radiobutton(label="No Facecam Ignore Zone",
                            variable=self.facecam_var, value="none")
        for val, label in (("tl", "Ignore Top-Left Corner"),
                           ("tr", "Ignore Top-Right Corner"),
                           ("bl", "Ignore Bottom-Left Corner"),
                           ("br", "Ignore Bottom-Right Corner")):
            fcm.add_radiobutton(label=label, variable=self.facecam_var,
                                value=val)
        fcm.add_radiobutton(label="Custom Zone (drag it)...",
                            variable=self.facecam_var, value="custom",
                            command=self._select_facecam_rect)
        advm.add_cascade(label="Facecam", menu=fcm)
        advm.add_checkbutton(label="Stream Is Mirror-Flipped",
                             variable=self.mirror_var)
        advm.add_separator()
        self.device_menu = tk.Menu(advm, tearoff=0)
        self._rebuild_device_menu([])
        advm.add_cascade(label="Listen On", menu=self.device_menu)
        self.streamapp_menu = tk.Menu(advm, tearoff=0)
        self._rebuild_streamapp_menu([])
        advm.add_cascade(label="Stream App", menu=self.streamapp_menu)
        advm.add_checkbutton(label="Show Stream App While Paused",
                             variable=self.swap_var,
                             command=self._save_config)
        advm.add_separator()
        advm.add_command(label="Show Last Capture Preview",
                         command=self.preview_win.deiconify)
        advm.add_separator()
        advm.add_command(label="Diagnostics...",
                         command=self._show_diagnostics)
        advm.add_command(label="Check for Updates...",
                         command=lambda: self._check_updates(quiet=False))
        m.add_cascade(label="Advanced", menu=advm)

        self.root.config(menu=m)
        self.root.bind_all("<Command-o>", lambda e: self._choose_file())
        self.root.bind_all("<Command-s>", lambda e: self._sync())
        self.root.bind_all("<Command-r>", lambda e: self._resync())
        self.root.bind_all("<Command-p>", lambda e: self._toggle_pause())
        self.root.bind_all("<Shift-Command-f>",
                           lambda e: self._toggle_fullscreen())

    # --------------------------------------------------------- diagnostics

    def _show_diagnostics(self):
        """The report in a window, with a button that copies it.

        Reading a diagnosis off a screen and retyping it is how detail
        gets lost, and detail is the entire point here - so the report is
        selectable, copyable in one click, and already on disk.
        """
        win = tk.Toplevel(self.root)
        win.title("StreamSync Diagnostics")
        win.geometry("760x520")

        frame = ttk.Frame(win, padding=8)
        frame.pack(fill="both", expand=True)

        text = tk.Text(frame, wrap="none", font=("Menlo", 11))
        ybar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        xbar = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        text.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        text.insert("1.0", "Collecting...")
        text.config(state="disabled")

        btns = ttk.Frame(win, padding=(8, 0, 8, 8))
        btns.pack(fill="x")
        status = ttk.Label(btns, text="")

        def fill(body):
            text.config(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", body)
            text.config(state="disabled")

        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(text.get("1.0", "end-1c"))
            status.config(text="Copied.")

        def reveal():
            subprocess.run(["open", "-R", diagnostics.LOG_FILE], check=False)

        ttk.Button(btns, text="Copy to Clipboard",
                   command=copy).pack(side="left")
        ttk.Button(btns, text="Show Log in Finder",
                   command=reveal).pack(side="left", padx=(8, 0))
        status.pack(side="left", padx=(8, 0))

        # Spotlight and codesign make this take a moment; collecting it on
        # the UI thread would look like the app had hung.
        def work():
            try:
                body = diagnostics.report()
                diagnostics.log_report("opened from the Advanced menu")
            except Exception as e:
                body = f"Could not collect diagnostics: {e!r}"
            self.root.after(0, lambda: fill(body))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ video

    def _build_video_window(self):
        """The window the film renders into.

        The first Mac field test proved playback runs fine with no window
        at all: a sync matched, audio decoded, and there was simply
        nothing to look at, because libvlc's macOS output needs to be
        handed an NSView and had not been. So the app now owns the video
        window - a Tk frame whose NSView is passed to libvlc - and shows
        it when a film loads.
        """
        self.video_win = tk.Toplevel(self.root)
        self.video_win.title("StreamSync - Video")
        self.video_win.geometry("960x540")
        self.video_win.configure(bg="black")
        self.video_frame = tk.Frame(self.video_win, bg="black")
        self.video_frame.pack(fill="both", expand=True)
        # Closing the window hides it; the film (and the sync) carry on.
        self.video_win.protocol("WM_DELETE_WINDOW", self.video_win.withdraw)
        self.video_win.withdraw()

    def _show_video_window(self):
        """Show the video window and hand its view to libvlc, once.

        winfo_id() is only meaningful after the widget really exists on
        screen, so attachment happens here, at first show, rather than at
        construction while the window is still withdrawn.
        """
        self.video_win.deiconify()
        self.video_win.lift()
        self.video_win.update_idletasks()
        if not self.player_backend.embedded:
            if not self.player_backend.attach_tk(self.video_frame.winfo_id()):
                # The pre-1.0.5 behavior, named instead of silent: sound
                # with no picture reads as a broken film otherwise.
                self._set_status(
                    "Video embedding is unavailable here - playback will "
                    "be audio-only. Advanced > Diagnostics... has details.")
                diagnostics.log("attach_tk failed: no NSView from Tk; "
                                "playback continues without video")

    # ------------------------------------------------------------- updates

    def _check_updates(self, quiet=True):
        """Ask GitHub for a newer release, off the UI thread.

        quiet=True is the startup check: it stays silent unless there is
        something to offer, so a laptop with no network never nags.
        """
        def work():
            try:
                tag, rel = updater.available_update()
            except updater.UpdateError as e:
                if not quiet:
                    self.q.put(("update_err", str(e)))
                return
            if tag:
                self.q.put(("update", tag, rel))
            elif not quiet:
                self.q.put(("update_none", __version__))

        threading.Thread(target=work, daemon=True).start()

    def _offer_update(self, tag, rel):
        app_path = updater.installed_app_path()
        if app_path is None:
            messagebox.showinfo(
                "StreamSync - update available",
                f"Version {tag} is available (you have {__version__}).\n\n"
                "This copy is running from source, so it can't replace "
                "itself - git pull instead.")
            return

        if not messagebox.askyesno(
                "StreamSync - update available",
                f"Version {tag} is available. You have {__version__}.\n\n"
                "StreamSync will download it, check it against the checksum "
                "published with the release, then restart into the new "
                "version. Your settings are kept.\n\nUpdate now?"):
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("StreamSync - updating")
        dlg.resizable(True, False)
        frm = ttk.Frame(dlg, padding=12)
        frm.grid(sticky="nsew")
        status = ttk.Label(frm, text="Starting...", width=52)
        status.grid(row=0, column=0, sticky="w")
        bar = ttk.Progressbar(frm, length=380, mode="determinate")
        bar.grid(row=1, column=0, pady=(8, 0))
        depcheck.present_window(dlg)

        def say(text):
            self.root.after(0, lambda: status.config(text=text))

        def work():
            try:
                asset = updater.pick_asset(rel.get("assets", []))
                sums = next((a["browser_download_url"]
                             for a in rel.get("assets", [])
                             if a.get("name") == updater.SUMS_ASSET), None)

                def progress(done, total):
                    if total:
                        pct = done * 100 // total
                        self.root.after(0, lambda: bar.config(value=pct))
                    self.root.after(0, lambda: status.config(
                        text=f"Downloading {asset['name']} - "
                             f"{done // 1024 // 1024} MB"))

                tmp = tempfile.mkdtemp(prefix="streamsync-dl-")
                dmg = updater.download_verified(asset, sums, tmp, progress)
                say("Checksum verified. Installing...")
                staged = updater.stage(dmg, app_path, say)
                say("Restarting into the new version...")
                updater.swap_and_relaunch(app_path, staged)
                self.root.after(300, self._on_close)
            except Exception as e:
                self.root.after(0, dlg.destroy)
                self.root.after(0, lambda: messagebox.showerror(
                    "StreamSync - update failed",
                    f"{e}\n\nYour installed copy has not been changed. You "
                    f"can download it yourself from:\n{updater.RELEASES_PAGE}"))

        threading.Thread(target=work, daemon=True).start()

    def _rebuild_subs_menu(self, tracks):
        self.subs_menu.delete(0, "end")
        if tracks:
            for tid, name in tracks:
                self.subs_menu.add_radiobutton(
                    label=name, variable=self.sub_var, value=str(tid),
                    command=lambda tid=tid: self._set_subtitle(tid))
            self.subs_menu.add_separator()
        self.subs_menu.add_command(label="Refresh Tracks",
                                   command=self._refresh_subs)

    def _rebuild_device_menu(self, names):
        self.device_menu.delete(0, "end")
        for n in names:
            self.device_menu.add_radiobutton(
                label=n, variable=self.device_var, value=n,
                command=self._on_device_pick)
        if names:
            self.device_menu.add_separator()
        self.device_menu.add_command(label="Refresh Devices",
                                     command=self._populate_audio_devices)

    def _rebuild_streamapp_menu(self, names):
        self.streamapp_menu.delete(0, "end")
        for n in names:
            self.streamapp_menu.add_radiobutton(
                label=n, variable=self.streamapp_var, value=n,
                command=self._on_streamapp_pick)
        if names:
            self.streamapp_menu.add_separator()
        self.streamapp_menu.add_command(label="Refresh Apps",
                                        command=self._refresh_stream_apps)

    # ------------------------------------------------------------- players

    @property
    def player(self):
        return self.active_player

    def _apply_player_choice(self):
        kind = self.player_var.get()
        if kind == "external":
            try:
                if self.external is None:
                    self.external = ExternalPlayer()
            except VLCError as e:
                messagebox.showerror("StreamSync", str(e))
                self.player_var.set("embedded")
                return
            self.player_backend.pause()
            self.active_player = self.external
            if self.video_path:
                t = self.player_backend.time()

                def spawn():
                    try:
                        self.external.load(self.video_path)
                        if t:
                            self.external.seek(t)
                        self.external.set_mute(self.mute_var.get())
                        self.q.put(("status", "Loaded in VLC.app - use "
                                              "Sync/Resync to line it up."))
                    except Exception as e:
                        self.q.put(("status", f"External VLC: {e}"))
                self._set_status("Starting VLC.app...")
                threading.Thread(target=spawn, daemon=True).start()
        else:
            if self.external is not None:
                self.external.pause()
            self.active_player = self.player_backend
        self._on_mute_toggle()
        self._save_config()

    # ------------------------------------------------------------- actions

    def _choose_file(self):
        path = filedialog.askopenfilename(
            title="Choose the local copy of the film",
            filetypes=[("Video files", "*.mkv *.mp4 *.avi *.mov *.m4v *.ts *.webm"),
                       ("All files", "*.*")])
        if not path:
            return
        self.video_path = path
        self.file_lbl.config(text=Path(path).name)
        if self.player is self.player_backend:
            self._show_video_window()
            try:
                self.player_backend.load(path)
            except VLCError as e:
                messagebox.showerror("StreamSync", str(e))
                return
            self._set_status("Loaded. Playback starts on first sync.")
        else:
            def spawn():
                try:
                    self.external.load(path)
                    self.q.put(("status", f"Loaded {Path(path).name} in VLC.app."))
                except Exception as e:
                    self.q.put(("status", f"External VLC: {e}"))
            threading.Thread(target=spawn, daemon=True).start()
        self._save_config()

    def _select_region(self):
        region = capture.RegionSelector(self.root).select()
        if region:
            self.region = region
            self._set_status(f"Capture region set ({region[2]}x{region[3]} px).")
            self._save_config()

    def _select_facecam_rect(self):
        if not self.region:
            messagebox.showinfo("StreamSync", "Set the capture region first "
                                              "(Advanced menu).")
            self.facecam_var.set("none")
            return
        rect = capture.RegionSelector(self.root).select()
        if not rect:
            self.facecam_var.set("none")
            return
        left, top, w, h = self.region
        x0 = max(0.0, min((rect[0] - left) / w, 1.0))
        y0 = max(0.0, min((rect[1] - top) / h, 1.0))
        x1 = max(0.0, min((rect[0] + rect[2] - left) / w, 1.0))
        y1 = max(0.0, min((rect[1] + rect[3] - top) / h, 1.0))
        if x1 - x0 < 0.02 or y1 - y0 < 0.02:
            messagebox.showinfo("StreamSync", "That zone is outside the "
                                              "capture region - try again.")
            self.facecam_var.set("none")
            return
        self.facecam_rect = (x0, y0, x1, y1)
        self._save_config()

    def _build_mask(self):
        mode = self.facecam_var.get()
        if mode in ("tl", "tr", "bl", "br"):
            return matcher.corner_mask(mode)
        if mode == "custom" and self.facecam_rect:
            return matcher.rect_mask(*self.facecam_rect)
        return None

    def _ready(self, need_region):
        if not self.video_path:
            messagebox.showinfo("StreamSync", "Open a film first (Cmd-O).")
            return False
        if need_region and not self.region:
            messagebox.showinfo("StreamSync", "Select the capture region "
                                              "first (Advanced menu).")
            return False
        return True

    def _sync(self):
        if self.busy:
            return
        method = self.method_var.get()
        if not self._ready(need_region=(method == "video")):
            return
        try:
            center = parse_time(self.hint_var.get())
            window = parse_time(self.window_var.get()) or 120.0
        except ValueError as e:
            messagebox.showerror("StreamSync", str(e))
            return
        if center is None:
            self._start_search(None, None)
        else:
            self._start_search(center - window, center + window)

    def _resync(self):
        if self.busy:
            return
        method = self.method_var.get()
        if not self._ready(need_region=(method == "video")):
            return
        t = self.player.time()
        if t is None:
            self._sync()
            return
        try:
            window = parse_time(self.window_var.get()) or 120.0
        except ValueError:
            window = 120.0
        self._start_search(t - window, t + 30.0)

    def _start_search(self, a, b):
        self.busy = True
        self.sync_btn.state(["disabled"])
        self.resync_btn.state(["disabled"])
        player = self.player
        offset = self.offset
        mute = self.mute_var.get()
        if self.method_var.get() == "audio":
            device = self.audio_device
            threading.Thread(
                target=self._audio_search_worker,
                args=(a, b, player, offset, mute, device), daemon=True).start()
        else:
            self._set_status("Capturing stream frames...")
            self.root.withdraw()  # our window must not cover the stream
            self.root.update()
            mask = self._build_mask()
            mirror = self.mirror_var.get()
            threading.Thread(
                target=self._video_search_worker,
                args=(a, b, player, offset, mute, mask, mirror),
                daemon=True).start()

    # ------------------------------------------------------------- workers

    def _audio_search_worker(self, a, b, player, offset, mute, device):
        try:
            self.q.put(("status",
                        f"Listening to the stream ({AUDIO_SYNC_SECONDS:.0f} s)..."))
            samples, sr, t0 = audio_capture.record_loopback(
                AUDIO_SYNC_SECONDS, speaker_name=device)
            feats = audio_matcher.prep_capture(samples, sr)
            match_t, score, z = audio_matcher.find_match_audio(
                self.video_path, feats, a, b,
                progress=lambda m: self.q.put(("status", m)))
            self.q.put(("status", "Seeking..."))
            player.sync_seek(match_t, t0, offset)
            player.set_mute(mute)
            self.q.put(("swap", False))
            self.q.put(("adone", match_t, score, z))
        except Exception as e:
            self.q.put(("error", str(e)))
        finally:
            self.q.put(("busy_off", None))

    def _video_search_worker(self, a, b, player, offset, mute, mask, mirror):
        try:
            time.sleep(0.3)  # let our window leave the screen
            burst_raw, t0 = capture.grab_burst(self.region, BURST_FRAMES,
                                               BURST_SPACING)
            self.q.put(("showroot", None))
            self.q.put(("preview", burst_raw[0][0]))
            burst = []
            for img, dt in burst_raw:
                if mirror:
                    img = np.fliplr(img)
                burst.append((matcher.prep_gray(img, mask), dt))
            match_t, score = matcher.find_match(
                self.video_path, burst, a, b,
                progress=lambda m: self.q.put(("status", m)), mask=mask)
            self.q.put(("status", "Seeking..."))
            player.sync_seek(match_t, t0, offset)
            player.set_mute(mute)
            self.q.put(("swap", False))
            self.q.put(("vdone", match_t, score))
        except Exception as e:
            self.q.put(("showroot", None))
            self.q.put(("error", str(e)))
        finally:
            self.q.put(("busy_off", None))

    # ------------------------------------------------------------ auto mode

    def _on_auto_toggle(self):
        self.auto_enabled = self.auto_var.get()
        self.auto_follow = self.follow_var.get()
        self.auto_interval = max(10, int(self.interval_var.get()))
        if self.auto_enabled:
            self._set_status(f"Auto re-sync on: every {self.auto_interval} s"
                             + (", following pauses." if self.auto_follow
                                else "."))
        self._save_config()

    def _auto_probe(self, lo, hi):
        try:
            samples, sr, t0 = audio_capture.record_loopback(
                AUTO_RECORD_SECONDS, speaker_name=self.audio_device)
            feats = audio_matcher.prep_capture(samples, sr)
            t, score, z = audio_matcher.find_match_audio(
                self.video_path, feats, lo, hi)
        except (RuntimeError, matcher.MatchError):
            return None
        if z >= audio_matcher.Z_OK and score >= audio_matcher.SCORE_OK:
            return t, score, z, t0
        return None

    def _auto_loop(self):
        mode = "normal"
        failures = 0
        pause_point = None
        next_at = 0.0
        while not self._closing:
            time.sleep(1.0)
            if (not self.auto_enabled or self.busy or not self.video_path
                    or self._session_running()):  # sessions own the playhead
                mode, failures = "normal", 0
                continue
            if time.monotonic() < next_at:
                continue
            player = self.active_player
            try:
                if mode == "normal":
                    t_ref = player.time() if player.is_playing() else None
                    if t_ref is None:
                        next_at = time.monotonic() + 5
                        continue
                    hit = self._auto_probe(t_ref - 45, t_ref + 50)
                    if hit:
                        t, score, z, t0 = hit
                        failures = 0
                        drift = t - t_ref
                        if abs(drift) > 0.35 and not self.busy:
                            player.sync_seek(t, t0, self.offset)
                            self.q.put(("status",
                                        f"Auto: corrected {drift:+.2f}s drift "
                                        f"(score {score:.2f}, z {z:.0f})."))
                        next_at = time.monotonic() + self.auto_interval
                    else:
                        failures += 1
                        if self.auto_follow and failures >= 2:
                            player.pause()
                            pause_point = t_ref
                            mode = "probe"
                            self.q.put(("swap", True))
                            self.q.put(("status",
                                        "Auto: film audio not found - assuming "
                                        "pause. Watching for resume..."))
                            next_at = time.monotonic() + 8
                        else:
                            next_at = time.monotonic() + 10
                else:
                    hit = self._auto_probe(pause_point - 25, pause_point + 40)
                    if hit:
                        t, score, z, t0 = hit
                        if not self.busy:
                            player.sync_seek(t, t0, self.offset)
                            self.q.put(("swap", False))
                            self.q.put(("status",
                                        "Auto: stream resumed - resynced."))
                        mode, failures = "normal", 0
                        next_at = time.monotonic() + self.auto_interval
                    else:
                        next_at = time.monotonic() + 8
            except Exception as e:
                self.q.put(("status", f"Auto-resync check failed: {e}"))
                next_at = time.monotonic() + max(self.auto_interval, 20)

    # ------------------------------------------------------------- playback

    def _nudge(self, delta):
        self.player.nudge(delta)
        self.offset += delta
        self.offset_lbl.config(text=f"  {self.offset:+.2f}s")

    def _toggle_pause(self):
        try:
            was_playing = self.player.is_playing()
        except Exception:
            was_playing = False
        self.player.toggle_pause()
        self._stream_swap(was_playing)

    def _on_mute_toggle(self):
        self.player.set_mute(self.mute_var.get())

    def _toggle_fullscreen(self):
        if self.player is self.player_backend:
            self.fullscreen = not self.fullscreen
            if self.player_backend.embedded:
                # The video lives in our own window now, so fullscreen is
                # the window's, not libvlc's - set_fullscreen only works
                # on a window libvlc itself owns.
                self.video_win.deiconify()
                self.video_win.attributes("-fullscreen", self.fullscreen)
            else:
                self.player_backend.set_fullscreen(self.fullscreen)
        else:
            self.external.fullscreen_toggle()

    # ---------------------------------------------------- hosted sessions

    def _session_running(self):
        return self.session is not None and not self.session.stop_flag.is_set()

    def _set_leave_enabled(self, enabled):
        self.session_menu.entryconfig(3, state="normal" if enabled else "disabled")

    def _host_dialog(self):
        if self._session_running():
            messagebox.showinfo("StreamSync", "Leave the current session first.")
            return
        if not self.video_path:
            messagebox.showinfo("StreamSync", "Open the film first (Cmd-O).")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Host a Session")
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=14)
        frm.grid()

        ttk.Label(frm, text="Relay server").grid(row=0, column=0, sticky="w")
        relay_var = tk.StringVar(value=self.relay_url)
        ttk.Entry(frm, textvariable=relay_var, width=32).grid(
            row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(frm, text="Password (optional)").grid(row=1, column=0,
                                                        sticky="w", pady=(6, 0))
        pw_var = tk.StringVar()
        ttk.Entry(frm, textvariable=pw_var, width=16).grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 0))

        ttk.Label(frm, text="Film position from").grid(row=2, column=0,
                                                       sticky="w", pady=(6, 0))
        src_var = tk.StringVar(value="listen")
        srcrow = ttk.Frame(frm)
        srcrow.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Radiobutton(srcrow, text="listening to this Mac", value="listen",
                        variable=src_var).pack(side="left")
        ttk.Radiobutton(srcrow, text="this app's player", value="player",
                        variable=src_var).pack(side="left", padx=(10, 0))

        ttk.Label(frm, text="Your microphone").grid(row=3, column=0,
                                                    sticky="w", pady=(6, 0))
        mic_var = tk.StringVar()
        mic_combo = ttk.Combobox(frm, textvariable=mic_var, width=30,
                                 state="readonly")
        mic_combo.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        try:
            mics = audio_capture.list_microphones()
            mic_combo["values"] = mics
            # prefer a real mic over BlackHole for the voice stream
            real = [n for n in mics if "blackhole" not in n.lower()]
            if real:
                mic_var.set(real[0])
            elif mics:
                mic_var.set(mics[0])
        except Exception:
            pass

        ttk.Label(frm, text="Delay hint for viewers (s)").grid(
            row=4, column=0, sticky="w", pady=(6, 0))
        delay_var = tk.IntVar(value=10)
        ttk.Spinbox(frm, from_=2, to=45, textvariable=delay_var,
                    width=6).grid(row=4, column=1, sticky="w",
                                  padx=(8, 0), pady=(6, 0))

        def start():
            self.relay_url = relay_var.get().strip()
            src = None
            if src_var.get() == "player":
                player = self.player
                src = lambda: (player.time(), player.is_playing())
            self.session = session.HostSession(
                self.relay_url, self.video_path, self.q,
                password=pw_var.get().strip() or None,
                position_source=src, mic_name=mic_var.get() or None,
                speaker_name=self.audio_device,
                default_delay=float(delay_var.get()),
                title=Path(self.video_path).stem)
            self.session.start()
            self._set_leave_enabled(True)
            self._save_config()
            dlg.destroy()

        ttk.Button(frm, text="Start Hosting", command=start).grid(
            row=5, column=1, sticky="e", pady=(12, 0))
        dlg.grab_set()

    def _join_dialog(self):
        if self._session_running():
            messagebox.showinfo("StreamSync", "Leave the current session first.")
            return
        if not self.video_path:
            messagebox.showinfo("StreamSync", "Open your copy of the film first "
                                              "(Cmd-O).")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Join a Session")
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=14)
        frm.grid()

        ttk.Label(frm, text="Relay server").grid(row=0, column=0, sticky="w")
        relay_var = tk.StringVar(value=self.relay_url)
        ttk.Entry(frm, textvariable=relay_var, width=32).grid(
            row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(frm, text="Session code").grid(row=1, column=0, sticky="w",
                                                 pady=(6, 0))
        code_var = tk.StringVar()
        ttk.Entry(frm, textvariable=code_var, width=14).grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Label(frm, text="Password").grid(row=2, column=0, sticky="w",
                                             pady=(6, 0))
        pw_var = tk.StringVar()
        ttk.Entry(frm, textvariable=pw_var, width=16).grid(
            row=2, column=1, sticky="w", padx=(8, 0), pady=(6, 0))

        def start():
            self.relay_url = relay_var.get().strip()
            self.session = session.ViewerSession(
                self.relay_url, code_var.get().strip().upper(),
                self.video_path, self.player, self.q,
                password=pw_var.get().strip() or None,
                speaker_name=self.audio_device)
            self.session.start()
            self.player.set_mute(self.mute_var.get())
            self._set_leave_enabled(True)
            self._save_config()
            dlg.destroy()

        ttk.Button(frm, text="Join", command=start).grid(
            row=3, column=1, sticky="e", pady=(12, 0))
        dlg.grab_set()

    def _leave_session(self):
        if self.session is not None:
            self.session.stop()
            self.session = None
        self._set_leave_enabled(False)
        self._set_status("Left the session.")

    # --------------------------------------------- facecam swap (AppleScript)

    def _refresh_stream_apps(self):
        def work():
            try:
                names = macwindowctl.list_gui_apps()
            except Exception as e:
                self.q.put(("status", f"Could not list apps: {e}"))
                return
            self.q.put(("apps", names))
        threading.Thread(target=work, daemon=True).start()

    def _on_streamapp_pick(self):
        self.stream_app = self.streamapp_var.get()
        self._save_config()

    def _stream_swap(self, show):
        if not self.swap_var.get() or show == self._swapped or not self.video_path:
            return
        app_name = self.stream_app
        if not app_name:
            try:
                running = macwindowctl.list_gui_apps()
                app_name = next((b for b in BROWSERS if b in running), "")
            except Exception:
                app_name = ""
        if not app_name:
            if show:
                self._set_status("Pick the stream's browser under Advanced > "
                                 "Stream App first.")
            return
        try:
            if show:
                self._was_fullscreen = self.fullscreen
                if self.player is self.player_backend and self.fullscreen:
                    self.player_backend.set_fullscreen(False)
                    self.fullscreen = False
                macwindowctl.activate_app(app_name)
                self._swapped = True
            else:
                macwindowctl.hide_app(app_name)
                if self.player is self.player_backend:
                    macwindowctl.activate_self()
                    if self._was_fullscreen:
                        self.player_backend.set_fullscreen(True)
                        self.fullscreen = True
                else:
                    macwindowctl.activate_app("VLC")
                self._swapped = False
        except Exception as e:
            self._set_status(f"App swap failed: {e} (grant Automation "
                             "permission in System Settings > Privacy).")

    # ------------------------------------------------------------ subtitles

    def _refresh_subs(self):
        if self.player is not self.player_backend:
            self._set_status("Use VLC.app's own Subtitles menu in external "
                             "mode.")
            return
        self._rebuild_subs_menu(self.player_backend.subtitle_tracks())

    def _set_subtitle(self, tid):
        try:
            self.player_backend.set_subtitle(tid)
        except Exception:
            pass

    def _load_sub_file(self):
        if self.player is not self.player_backend:
            self._set_status("Load subtitles through VLC.app's menu in "
                             "external mode.")
            return
        path = filedialog.askopenfilename(
            title="Choose a subtitle file",
            filetypes=[("Subtitles", "*.srt *.ass *.ssa *.sub *.vtt"),
                       ("All files", "*.*")])
        if path:
            self.player_backend.add_subtitle_file(path)
            self.root.after(600, self._refresh_subs)

    # ------------------------------------------------------------- plumbing

    def _poll_queue(self):
        try:
            while True:
                kind, *payload = self.q.get_nowait()
                if kind == "status":
                    self._set_status(payload[0])
                elif kind == "session":
                    self._set_status(payload[0])
                elif kind == "swap":
                    self._stream_swap(payload[0])
                elif kind == "showroot":
                    self.root.deiconify()
                elif kind == "devices":
                    self._rebuild_device_menu(payload[0])
                    if not self.device_var.get() and payload[0]:
                        self.device_var.set(payload[0][0])
                        self._on_device_pick()
                elif kind == "apps":
                    self._rebuild_streamapp_menu(payload[0])
                elif kind == "preview":
                    self._show_preview(payload[0])
                elif kind == "update":
                    self._offer_update(payload[0], payload[1])
                elif kind == "update_none":
                    messagebox.showinfo(
                        "StreamSync",
                        f"You're on the latest version ({payload[0]}).")
                elif kind == "update_err":
                    messagebox.showwarning(
                        "StreamSync - update check failed", payload[0])
                elif kind == "adone":
                    match_t, score, z = payload
                    msg = (f"Matched at {fmt_time(match_t)} "
                           f"(score {score:.2f}, z {z:.0f}).")
                    if score < audio_matcher.SCORE_OK or z < audio_matcher.Z_OK:
                        msg += (" Weak - check BlackHole routing, or try a "
                                "louder scene.")
                    self._set_status(msg)
                elif kind == "vdone":
                    match_t, score = payload
                    msg = (f"Matched at {fmt_time(match_t)} "
                           f"(confidence {score:.2f}).")
                    if score < LOW_CONFIDENCE:
                        msg += " Low confidence - check region/facecam zone."
                    self._set_status(msg)
                elif kind == "error":
                    self._set_status(f"Sync failed: {payload[0]}")
                elif kind == "busy_off":
                    self.busy = False
                    self.sync_btn.state(["!disabled"])
                    self.resync_btn.state(["!disabled"])
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    def _populate_audio_devices(self):
        def work():
            try:
                names = audio_capture.list_speakers()
            except Exception as e:
                self.q.put(("status", f"Could not list audio inputs: {e}"))
                return
            self.q.put(("devices", names))
        threading.Thread(target=work, daemon=True).start()

    def _on_device_pick(self):
        self.audio_device = self.device_var.get()
        self._save_config()

    def _show_preview(self, gray_img):
        img = Image.fromarray((gray_img * 255).clip(0, 255).astype("uint8"))
        img.thumbnail((320, 180))
        self._preview_photo = ImageTk.PhotoImage(img)
        self.preview_lbl.config(image=self._preview_photo, text="")

    def _set_status(self, text):
        self.status_lbl.config(text=text)

    def _tick_time(self):
        try:
            t, n = self.player.time(), self.player.length()
            state = "playing" if self.player.is_playing() else "paused"
            if t is not None:
                total = f" / {fmt_time(n)}" if n else ""
                self.time_lbl.config(text=f"{fmt_time(t)}{total}  ({state})")
        except Exception:
            pass
        self.root.after(700, self._tick_time)

    def _save_config(self):
        try:
            CONFIG_PATH.write_text(json.dumps({
                "video_path": self.video_path,
                "region": self.region,
                "window": self.window_var.get(),
                "hint": self.hint_var.get(),
                "method": self.method_var.get(),
                "player": self.player_var.get(),
                "audio_device": self.audio_device,
                "facecam": self.facecam_var.get(),
                "facecam_rect": self.facecam_rect,
                "mirror": self.mirror_var.get(),
                "auto_interval": self.auto_interval,
                "auto_follow": self.follow_var.get(),
                "swap": self.swap_var.get(),
                "stream_app": self.stream_app,
                "relay_url": self.relay_url,
            }))
        except OSError:
            pass

    def _load_config(self):
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except (OSError, ValueError):
            return
        path = cfg.get("video_path")
        if path and Path(path).is_file():
            self.video_path = path
            # The same show-then-load as _choose_file: this is the path a
            # second launch takes, and it was the one that played films
            # into a window that did not exist.
            self._show_video_window()
            self.player_backend.load(path)
            self.file_lbl.config(text=Path(path).name)
        region = cfg.get("region")
        if region and len(region) == 4:
            self.region = tuple(int(v) for v in region)
        if cfg.get("window"):
            self.window_var.set(cfg["window"])
        if cfg.get("hint"):
            self.hint_var.set(cfg["hint"])
        if cfg.get("method") in ("audio", "video"):
            self.method_var.set(cfg["method"])
        self.audio_device = cfg.get("audio_device", "")
        if self.audio_device:
            self.device_var.set(self.audio_device)
        if cfg.get("facecam"):
            self.facecam_var.set(cfg["facecam"])
        rect = cfg.get("facecam_rect")
        if rect and len(rect) == 4:
            self.facecam_rect = tuple(float(v) for v in rect)
        self.mirror_var.set(bool(cfg.get("mirror", False)))
        try:
            self.auto_interval = max(10, int(cfg.get("auto_interval", 30)))
        except (TypeError, ValueError):
            self.auto_interval = 30
        self.interval_var.set(self.auto_interval)
        self.follow_var.set(bool(cfg.get("auto_follow", True)))
        self.auto_follow = self.follow_var.get()
        self.swap_var.set(bool(cfg.get("swap", True)))
        self.stream_app = cfg.get("stream_app", "")
        if self.stream_app:
            self.streamapp_var.set(self.stream_app)
        if cfg.get("relay_url"):
            self.relay_url = cfg["relay_url"]

    def _on_close(self):
        self._closing = True
        if self.session is not None:
            try:
                self.session.stop()
            except Exception:
                pass
        self._save_config()
        try:
            self.player_backend.stop()
        except Exception:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    MacApp(root)
    # Same exposure the first-run dialog had: launched from Finder this
    # window can come up titled but unpainted until something forces a draw.
    depcheck.present_window(root)
    if "--selftest" in sys.argv:
        root.after(3000, root.destroy)
    root.mainloop()
    if "--selftest" in sys.argv:
        print("SELFTEST OK")
        # libvlc leaves native threads running that a plain return would wait
        # on; an unattended check must never be able to hang.
        sys.stdout.flush()
        os._exit(0)


if __name__ == "__main__":
    main()
