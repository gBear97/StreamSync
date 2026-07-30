"""StreamSync - sync a local copy of a film in VLC to a live stream.

Primary method: record a few seconds of system audio (the stream's sound,
commentary included) via WASAPI loopback and find the matching moment in
the local file's audio track - works with the stream minimized. Fallback
method: screen-capture frame matching with facecam ignore-zones.

Playback goes to an embedded VLC surface (millisecond seeks) or to the
real VLC app driven over its HTTP interface. An optional auto mode
re-checks sync in the background, corrects drift, and can follow the
streamer's pauses.
"""

import ctypes
import json
import logging
import queue
import sys
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
import matcher
import players
import session
import windowctl
from players import EmbeddedPlayer, ExternalPlayer, VLCError

CONFIG_PATH = Path.home() / ".streamsync.json"
log = logging.getLogger("streamsync.app")
BURST_FRAMES = 4
BURST_SPACING = 1 / 3      # seconds between captured frames (aligns with 12 fps scan)
AUDIO_SYNC_SECONDS = 6.0   # manual sync recording length
AUTO_RECORD_SECONDS = 4.0  # auto-mode recording length
PROBE_MAX_AHEAD = 600.0    # cap the growing resume-search window (_auto_loop)
LOW_CONFIDENCE = 0.55      # video-match warning threshold


def fmt_time(s):
    s = max(0.0, float(s))
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    frac = s - int(s)
    if h:
        return f"{h}:{m:02d}:{sec + frac:04.1f}"
    return f"{m}:{sec + frac:04.1f}"


def parse_time(text):
    """'1:23:45', '23:45', '95' -> seconds; empty -> None."""
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


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.region = None
        self.video_path = None
        self.busy = False
        self.offset = 0.0
        self.fullscreen = False
        self.facecam_rect = None     # normalized (x0, y0, x1, y1) ignore zone
        self.audio_device = ""       # substring of speaker name; "" = default
        self.auto_enabled = False
        self.auto_follow = True
        self.auto_interval = 30
        self._closing = False
        self._preview_photo = None
        self.external = None         # created on demand
        self.stream_hwnd = None      # pinned stream browser window
        self.stream_title = ""
        self._win_map = {}
        self._swapped = False        # stream window currently shown?
        self._was_fullscreen = False
        self._ext_hwnd = None
        self.session = None          # active HostSession / ViewerSession
        self.relay_url = "ws://localhost:8765"
        self._osd_win = None         # Tk-drawn volume OSD over the video
        self._osd_after = None
        self._fs_saved = None        # frame style + rect to restore from

        root.title("StreamSync")
        root.resizable(False, False)

        self._build_video_window()
        self._build_controls()
        self._build_video_menu()  # after controls: shares their variables

        try:
            self.embedded = EmbeddedPlayer(self.video_frame.winfo_id())
        except VLCError as e:
            messagebox.showerror("StreamSync - VLC problem", str(e))
            raise SystemExit(1)
        self.active_player = self.embedded
        self._shield_video_input()

        self._load_config()
        self._apply_method_visibility()
        self._populate_audio_devices()
        self._refresh_windows()
        self._install_hotkeys()

        threading.Thread(target=self._auto_loop, daemon=True).start()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        # frozen builds have no console: Tk callback errors would vanish
        root.report_callback_exception = \
            lambda *exc: log.error("Tk callback error", exc_info=exc)
        root.after(80, self._poll_queue)
        root.after(700, self._tick_time)
        log.info("app up: video=%r method=%s auto=%s",
                 self.video_path, self.method_var.get(), self.auto_enabled)

    # ------------------------------------------------------------- UI setup

    def _build_video_window(self):
        self.video_win = tk.Toplevel(self.root)
        self.video_win.title("StreamSync - video")
        self.video_win.geometry("960x540+80+80")
        self.video_win.configure(bg="black")
        self.video_frame = tk.Frame(self.video_win, bg="black")
        self.video_frame.pack(fill="both", expand=True)
        self.video_win.bind("<Escape>", lambda e: self._set_fullscreen(False))
        self.video_win.bind("<F11>", lambda e: self._set_fullscreen(not self.fullscreen))
        self.video_win.bind("<space>", lambda e: self._toggle_pause())
        # libvlc's vout child forwards mouse messages to us synchronously -
        # its window thread blocks until the handler returns. Touching the
        # window (fullscreen) or the vout (volume OSD) from inside that
        # context deadlocks against the blocked sender, so every mouse
        # handler defers its work to the next event-loop pass.
        self.video_win.bind("<Double-Button-1>",
                            lambda e: self.video_win.after(
                                1, self._fullscreen_clicked))
        self.video_win.bind("<MouseWheel>", self._on_video_wheel)
        self.video_win.bind("<Button-3>", self._show_video_menu)
        # a click on the video focuses the vlc child, which would eat the
        # wheel and the space/F11 bindings from then on - reclaim focus for
        # the toplevel whenever the pointer enters
        self.video_win.bind("<Enter>", lambda e: self.video_win.focus_set())
        self.video_win.protocol("WM_DELETE_WINDOW", self.video_win.withdraw)
        self.video_win.withdraw()
        self.video_win.update_idletasks()

    def _build_controls(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.grid(sticky="nsew")

        # ---------------- Setup
        setup = ttk.LabelFrame(outer, text="Setup", padding=8)
        setup.grid(row=0, column=0, sticky="ew")
        row = ttk.Frame(setup)
        row.pack(fill="x")
        ttk.Button(row, text="Video file...", width=16,
                   command=self._choose_file).pack(side="left")
        self.file_lbl = ttk.Label(row, text="no file selected")
        self.file_lbl.pack(side="left", padx=(8, 0))

        row = ttk.Frame(setup)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="Player:").pack(side="left")
        self.player_var = tk.StringVar(value="embedded")
        ttk.Radiobutton(row, text="Embedded (precise sync)", value="embedded",
                        variable=self.player_var,
                        command=self._apply_player_choice).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(row, text="External VLC app", value="external",
                        variable=self.player_var,
                        command=self._apply_player_choice).pack(side="left", padx=(10, 0))

        row = ttk.Frame(setup)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="Sync by:").pack(side="left")
        self.method_var = tk.StringVar(value="audio")
        ttk.Radiobutton(row, text="Audio (works minimized)", value="audio",
                        variable=self.method_var,
                        command=self._apply_method_visibility).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(row, text="Video screen-capture (experimental)", value="video",
                        variable=self.method_var,
                        command=self._apply_method_visibility).pack(side="left", padx=(10, 0))

        # audio-method row
        self.audio_row = ttk.Frame(setup)
        self.audio_row.pack(fill="x", pady=(6, 0))
        ttk.Label(self.audio_row, text="Listen on:").pack(side="left")
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(self.audio_row, textvariable=self.device_var,
                                         width=42, state="readonly")
        self.device_combo.pack(side="left", padx=(6, 0))
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_pick)
        ttk.Button(self.audio_row, text="Refresh", width=8,
                   command=self._populate_audio_devices).pack(side="left", padx=(6, 0))

        # video-method row
        self.video_row = ttk.Frame(setup)
        self.video_row.pack(fill="x", pady=(6, 0))
        ttk.Button(self.video_row, text="Capture region...", width=16,
                   command=self._select_region).pack(side="left")
        self.region_lbl = ttk.Label(self.video_row, text="not set")
        self.region_lbl.pack(side="left", padx=(8, 0))
        self.adv_btn = ttk.Menubutton(self.video_row, text="Edge cases")
        self.adv_btn.pack(side="left", padx=(10, 0))
        self.preview_lbl = ttk.Label(self.video_row)
        self.preview_lbl.pack(side="right")
        self._build_advanced_menu()

        # subtitles (embedded player only)
        self.subs_row = ttk.Frame(setup)
        self.subs_row.pack(fill="x", pady=(6, 0))
        ttk.Label(self.subs_row, text="Subtitles:").pack(side="left")
        self.sub_var = tk.StringVar()
        self.sub_combo = ttk.Combobox(self.subs_row, textvariable=self.sub_var,
                                      width=32, state="readonly")
        self.sub_combo.pack(side="left", padx=(6, 0))
        self.sub_combo.bind("<<ComboboxSelected>>", self._on_sub_pick)
        ttk.Button(self.subs_row, text="Refresh", width=8,
                   command=self._refresh_subs).pack(side="left", padx=(6, 0))
        ttk.Button(self.subs_row, text="Load file...", width=10,
                   command=self._load_sub_file).pack(side="left", padx=(6, 0))

        # ---------------- Sync
        sync = ttk.LabelFrame(outer, text="Sync", padding=8)
        sync.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        row = ttk.Frame(sync)
        row.pack(fill="x")
        ttk.Label(row, text="Position hint").pack(side="left")
        self.hint_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.hint_var, width=10).pack(side="left", padx=(6, 0))
        ttk.Label(row, text="(h:mm:ss, blank = whole file)   Search +/-").pack(
            side="left", padx=(6, 0))
        self.window_var = tk.StringVar(value="2:00")
        ttk.Entry(row, textvariable=self.window_var, width=8).pack(side="left", padx=(6, 0))

        row = ttk.Frame(sync)
        row.pack(fill="x", pady=(8, 0))
        self.sync_btn = ttk.Button(row, text="Sync to stream", command=self._sync)
        self.sync_btn.pack(side="left")
        self.resync_btn = ttk.Button(row, text="Resync (around current position)",
                                     command=self._resync)
        self.resync_btn.pack(side="left", padx=(8, 0))

        row = ttk.Frame(sync)
        row.pack(fill="x", pady=(8, 0))
        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Auto re-sync (audio) every",
                        variable=self.auto_var,
                        command=self._on_auto_toggle).pack(side="left")
        self.interval_var = tk.IntVar(value=30)
        sp = ttk.Spinbox(row, from_=10, to=300, increment=5, width=5,
                         textvariable=self.interval_var, command=self._on_auto_toggle)
        sp.pack(side="left", padx=(4, 0))
        ttk.Label(row, text="s").pack(side="left", padx=(2, 0))
        self.follow_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="follow stream pauses",
                        variable=self.follow_var,
                        command=self._on_auto_toggle).pack(side="left", padx=(12, 0))

        row = ttk.Frame(sync)
        row.pack(fill="x", pady=(8, 0))
        self.swap_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="Show stream window while paused (facecam)",
                        variable=self.swap_var,
                        command=self._save_config).pack(side="left")
        row = ttk.Frame(sync)
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text="Stream window:").pack(side="left")
        self.streamwin_var = tk.StringVar()
        self.streamwin_combo = ttk.Combobox(row, textvariable=self.streamwin_var,
                                            width=46, state="readonly")
        self.streamwin_combo.pack(side="left", padx=(6, 0))
        self.streamwin_combo.bind("<<ComboboxSelected>>", self._on_streamwin_pick)
        ttk.Button(row, text="Refresh", width=8,
                   command=self._refresh_windows).pack(side="left", padx=(6, 0))

        # ---------------- Watch party (hosted sessions)
        party = ttk.LabelFrame(outer, text="Watch party (hosted session)", padding=8)
        party.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        row = ttk.Frame(party)
        row.pack(fill="x")
        self.host_btn = ttk.Button(row, text="Host a session...",
                                   command=self._host_dialog)
        self.host_btn.pack(side="left")
        self.join_btn = ttk.Button(row, text="Join a session...",
                                   command=self._join_dialog)
        self.join_btn.pack(side="left", padx=(8, 0))
        self.leave_btn = ttk.Button(row, text="Leave", command=self._leave_session)
        self.leave_btn.pack(side="left", padx=(8, 0))
        self.leave_btn.state(["disabled"])
        self.session_lbl = ttk.Label(party, text="No session. Host: stream only "
                                                 "your voice; viewers sync their own copies.",
                                     foreground="#666", wraplength=600)
        self.session_lbl.pack(fill="x", pady=(6, 0))

        # ---------------- Playback
        play = ttk.LabelFrame(outer, text="Playback", padding=8)
        play.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        row = ttk.Frame(play)
        row.pack(fill="x")
        ttk.Button(row, text="Play / Pause", command=self._toggle_pause).pack(side="left")
        self.mute_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="Mute local audio",
                        variable=self.mute_var,
                        command=self._on_mute_toggle).pack(side="left", padx=(10, 0))
        ttk.Button(row, text="Fullscreen",
                   command=self._fullscreen_clicked).pack(side="left", padx=(10, 0))
        self.show_video_btn = ttk.Button(row, text="Show video window",
                                         command=self.video_win.deiconify)
        self.show_video_btn.pack(side="left", padx=(10, 0))

        row = ttk.Frame(play)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Nudge:").pack(side="left")
        for d in (-2.0, -0.5, -0.1, 0.1, 0.5, 2.0):
            ttk.Button(row, text=f"{d:+g}s", width=6,
                       command=lambda d=d: self._nudge(d)).pack(side="left", padx=2)
        self.offset_lbl = ttk.Label(row, text="offset: +0.00s")
        self.offset_lbl.pack(side="left", padx=(10, 0))
        ttk.Button(row, text="Reset", width=6,
                   command=self._reset_offset).pack(side="left", padx=(4, 0))

        self.time_lbl = ttk.Label(outer, text="-:-- / -:--")
        self.time_lbl.grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.status_lbl = ttk.Label(outer, text="Pick a video file to begin.",
                                    wraplength=620, foreground="#245")
        self.status_lbl.grid(row=5, column=0, sticky="w", pady=(4, 0))
        self.hotkey_lbl = ttk.Label(outer, text="", foreground="#666")
        self.hotkey_lbl.grid(row=6, column=0, sticky="w", pady=(4, 0))

    def _build_advanced_menu(self):
        menu = tk.Menu(self.adv_btn, tearoff=0)
        self.facecam_var = tk.StringVar(value="none")
        menu.add_radiobutton(label="No facecam ignore zone",
                             variable=self.facecam_var, value="none")
        for val, label in (("tl", "Ignore top-left corner (facecam)"),
                           ("tr", "Ignore top-right corner (facecam)"),
                           ("bl", "Ignore bottom-left corner (facecam)"),
                           ("br", "Ignore bottom-right corner (facecam)")):
            menu.add_radiobutton(label=label, variable=self.facecam_var, value=val)
        menu.add_radiobutton(label="Custom ignore zone (drag it)...",
                             variable=self.facecam_var, value="custom",
                             command=self._select_facecam_rect)
        menu.add_separator()
        self.mirror_var = tk.BooleanVar(value=False)
        menu.add_checkbutton(label="Stream is mirror-flipped",
                             variable=self.mirror_var)
        self.adv_btn.config(menu=menu)

    def _apply_method_visibility(self):
        # subs_row is always packed, so it works as a stable anchor
        if self.method_var.get() == "audio":
            self.video_row.pack_forget()
            self.audio_row.pack(fill="x", pady=(6, 0), before=self.subs_row)
        else:
            self.audio_row.pack_forget()
            self.video_row.pack(fill="x", pady=(6, 0), before=self.subs_row)

    # ------------------------------------------------------------- players

    def _apply_player_choice(self):
        kind = self.player_var.get()
        log.info("player choice -> %s", kind)
        if kind == "external":
            try:
                if self.external is None:
                    self.external = ExternalPlayer()
            except VLCError as e:
                messagebox.showerror("StreamSync", str(e))
                self.player_var.set("embedded")
                return
            self.embedded.pause()
            self.video_win.withdraw()
            self.active_player = self.external
            if self.video_path:
                t = self.embedded.time()
                self._set_status("Starting external VLC...")

                def spawn():
                    try:
                        self.external.load(self.video_path)
                        if t:
                            self.external.seek(t)
                        self.q.put(("status",
                                    "Loaded in external VLC - use Sync/Resync to "
                                    "line it up. Use VLC's own menus for subtitles."))
                        self.external.set_mute(self.mute_var.get())
                    except Exception as e:
                        self.q.put(("status", f"External VLC: {e}"))
                threading.Thread(target=spawn, daemon=True).start()
        else:
            if self.external is not None:
                self.external.pause()
            self.active_player = self.embedded
            if self.video_path:
                self.video_win.deiconify()
        self._on_mute_toggle()
        self._save_config()

    @property
    def player(self):
        return self.active_player

    # ------------------------------------------------------------- actions

    def _choose_file(self):
        path = filedialog.askopenfilename(
            title="Choose the local copy of the film",
            filetypes=[("Video files", "*.mkv *.mp4 *.avi *.mov *.m4v *.ts *.webm *.wmv"),
                       ("All files", "*.*")])
        if not path:
            return
        self.video_path = path
        log.info("file chosen: %r (player=%s)", path,
                 "embedded" if self.player is self.embedded else "external")
        self.file_lbl.config(text=Path(path).name)
        if self.player is self.embedded:
            try:
                self.embedded.load(path)
            except VLCError as e:
                messagebox.showerror("StreamSync", str(e))
                return
            self.video_win.deiconify()
            self._set_status(f"Loaded {Path(path).name}. Playback starts on first sync.")
        else:
            def spawn():
                try:
                    self.external.load(path)
                    self.q.put(("status", f"Loaded {Path(path).name} in external VLC."))
                except Exception as e:
                    self.q.put(("status", f"External VLC: {e}"))
            self._set_status("Opening in external VLC...")
            threading.Thread(target=spawn, daemon=True).start()
        self._save_config()

    def _select_region(self):
        region = capture.RegionSelector(self.root).select()
        if region:
            self.region = region
            self.region_lbl.config(
                text=f"{region[2]}x{region[3]} at ({region[0]}, {region[1]})")
            self._set_status("Region set.")
            self._save_config()

    def _select_facecam_rect(self):
        if not self.region:
            messagebox.showinfo("StreamSync",
                                "Set the capture region first, then drag the "
                                "facecam zone inside it.")
            self.facecam_var.set("none")
            return
        rect = capture.RegionSelector(self.root).select()
        if not rect:
            self.facecam_var.set("none")
            return
        left, top, w, h = self.region
        x0 = (rect[0] - left) / w
        y0 = (rect[1] - top) / h
        x1 = (rect[0] + rect[2] - left) / w
        y1 = (rect[1] + rect[3] - top) / h
        x0, x1 = max(0.0, min(x0, 1.0)), max(0.0, min(x1, 1.0))
        y0, y1 = max(0.0, min(y0, 1.0)), max(0.0, min(y1, 1.0))
        if x1 - x0 < 0.02 or y1 - y0 < 0.02:
            messagebox.showinfo("StreamSync", "That zone is outside the capture "
                                              "region - try again.")
            self.facecam_var.set("none")
            return
        self.facecam_rect = (x0, y0, x1, y1)
        self._set_status(f"Ignoring zone x {x0:.2f}-{x1:.2f}, y {y0:.2f}-{y1:.2f} "
                         "of the frame during video matching.")
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
            messagebox.showinfo("StreamSync", "Choose a video file first.")
            return False
        if need_region and not self.region:
            messagebox.showinfo("StreamSync", "Select the stream's capture region first.")
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
        # a paused stream leaves the local copy ahead, so look mostly backwards
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
            hidden = self._hide_overlapping_windows()
            mask = self._build_mask()
            mirror = self.mirror_var.get()
            threading.Thread(
                target=self._video_search_worker,
                args=(a, b, player, offset, mute, hidden, mask, mirror),
                daemon=True).start()

    # ------------------------------------------------------------- workers

    def _audio_search_worker(self, a, b, player, offset, mute, device):
        try:
            self.q.put(("status",
                        f"Recording stream audio ({AUDIO_SYNC_SECONDS:.0f} s)..."))
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

    def _video_search_worker(self, a, b, player, offset, mute, hidden,
                             mask, mirror):
        try:
            if hidden:
                time.sleep(0.3)  # let withdrawn windows actually leave the screen
            burst_raw, t0 = capture.grab_burst(self.region, BURST_FRAMES,
                                               BURST_SPACING)
            self.q.put(("show", hidden))
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
            self.q.put(("show", hidden))
            self.q.put(("error", str(e)))
        finally:
            self.q.put(("busy_off", None))

    def _hide_overlapping_windows(self):
        left, top, w, h = self.region
        hidden = []
        for win in (self.root, self.video_win):
            if win.state() == "withdrawn":
                continue
            wx, wy = win.winfo_rootx(), win.winfo_rooty()
            ww, wh = win.winfo_width(), win.winfo_height()
            if wx < left + w and wx + ww > left and wy < top + h and wy + wh > top:
                win.withdraw()
                hidden.append(win)
        if hidden:
            self.root.update()
        return hidden

    # ------------------------------------------------------------- auto mode

    def _on_auto_toggle(self):
        self.auto_enabled = self.auto_var.get()
        self.auto_follow = self.follow_var.get()
        try:
            self.auto_interval = max(10, int(self.interval_var.get()))
        except (ValueError, tk.TclError):
            self.auto_interval = 30
        if self.auto_enabled:
            self._set_status(f"Auto re-sync on: checking every "
                             f"{self.auto_interval} s"
                             + (", following pauses." if self.auto_follow else "."))
        self._save_config()

    def _clock_lag(self):
        """How far the active player's clock trails what it is emitting.

        Zero on this platform until someone measures it: the constant was
        established on macOS (see players.CLOCK_OUTPUT_LAG). If it turns
        out to be non-zero here too, setting it there is the whole fix.
        """
        return (players.CLOCK_OUTPUT_LAG
                if self.active_player is self.embedded else 0.0)

    def _auto_probe(self, lo, hi):
        """One auto-mode listen+match attempt.

        Returns (t, score, z, t0_perf) on a confident match, else None.
        Silence counts as no-match: a silent stream is a paused stream as
        far as syncing is concerned.
        """
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
        paused_at = 0.0
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
                    t_ref_at = time.perf_counter()
                    if t_ref is None:
                        next_at = time.monotonic() + 5
                        continue
                    hit = self._auto_probe(t_ref - 45, t_ref + 50)
                    if hit:
                        t, score, z, t0 = hit
                        failures = 0
                        # `t` is where the stream was when the capture
                        # STARTED, so measure the film at that same
                        # instant: t_ref predates it by however long the
                        # capture device took to open, and the probe runs
                        # for seconds after that.
                        now_pos = player.time()
                        moved = None if now_pos is None else now_pos - t_ref
                        if (now_pos is None or not player.is_playing()
                                or abs(moved - (time.perf_counter() - t_ref_at))
                                > 0.5):
                            # Paused, seeked or stalled while we listened -
                            # winding the clock back from now would invent a
                            # position. Say nothing and look again shortly.
                            next_at = time.monotonic() + 5
                            continue
                        film_at_t0 = now_pos - (time.perf_counter() - t0)
                        # Where the film SHOULD be: the stream's position
                        # plus the offset the user nudged in, which every
                        # sync_seek applies. Ignoring it would read a
                        # deliberate offset as drift and undo it on a loop.
                        drift = ((t + self.offset)
                                 - (film_at_t0 + self._clock_lag()))
                        if abs(drift) > 0.35 and not self.busy \
                                and not self._session_running():
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
                            paused_at = time.monotonic()
                            mode = "probe"
                            self.q.put(("swap", True))
                            self.q.put(("status",
                                        "Auto: film audio not found on the stream "
                                        "- assuming pause. Watching for resume..."))
                            next_at = time.monotonic() + 8
                        else:
                            next_at = time.monotonic() + 10
                else:  # probe: paused, waiting for the stream to resume
                    # A real pause resumes near pause_point, so look there
                    # first - but the "pause" may have been a false alarm
                    # (quiet dialog under loud commentary) and the stream
                    # kept running. Grow the forward edge with elapsed time
                    # so it keeps up with a stream that never stopped.
                    ahead = min(time.monotonic() - paused_at, PROBE_MAX_AHEAD)
                    hit = self._auto_probe(pause_point - 25,
                                           pause_point + 40 + ahead)
                    if hit:
                        t, score, z, t0 = hit
                        if self.busy or self._session_running():
                            # Someone else is driving the playhead. Stay in
                            # probe mode and try again shortly: dropping to
                            # normal now would leave the film paused, and
                            # normal mode ignores a film that is not
                            # playing - the loop would idle for good.
                            next_at = time.monotonic() + 5
                            continue
                        player.sync_seek(t, t0, self.offset)
                        self.q.put(("swap", False))
                        self.q.put(("status", "Auto: stream resumed - resynced."))
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
        self.offset_lbl.config(text=f"offset: {self.offset:+.2f}s")

    def _reset_offset(self):
        self.offset = 0.0
        self.offset_lbl.config(text="offset: +0.00s")

    def _toggle_pause(self):
        try:
            was_playing = self.player.is_playing()
        except Exception:
            was_playing = False
        self.player.toggle_pause()
        # pausing brings the streamer's window up; resuming brings the film back
        self._stream_swap(was_playing)

    # ---------------------------------------------------- hosted sessions

    def _session_running(self):
        return self.session is not None and not self.session.stop_flag.is_set()

    def _host_dialog(self):
        if self._session_running():
            messagebox.showinfo("StreamSync", "Leave the current session first.")
            return
        if not self.video_path:
            messagebox.showinfo("StreamSync", "Choose the film file first.")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Host a session")
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=12)
        frm.grid()

        ttk.Label(frm, text="Relay server").grid(row=0, column=0, sticky="w")
        relay_var = tk.StringVar(value=self.relay_url)
        ttk.Entry(frm, textvariable=relay_var, width=34).grid(
            row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(frm, text="Password (optional)").grid(row=1, column=0,
                                                        sticky="w", pady=(6, 0))
        pw_var = tk.StringVar()
        ttk.Entry(frm, textvariable=pw_var, width=18).grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 0))

        ttk.Label(frm, text="Film position from").grid(row=2, column=0,
                                                       sticky="w", pady=(6, 0))
        src_var = tk.StringVar(value="listen")
        srcrow = ttk.Frame(frm)
        srcrow.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Radiobutton(srcrow, text="listening to this PC", value="listen",
                        variable=src_var).pack(side="left")
        ttk.Radiobutton(srcrow, text="this app's player", value="player",
                        variable=src_var).pack(side="left", padx=(10, 0))

        ttk.Label(frm, text="Your microphone").grid(row=3, column=0,
                                                    sticky="w", pady=(6, 0))
        mic_var = tk.StringVar()
        mic_combo = ttk.Combobox(frm, textvariable=mic_var, width=32,
                                 state="readonly")
        mic_combo.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        try:
            mics = audio_capture.list_microphones()
            mic_combo["values"] = mics
            if mics:
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
                default_delay=float(delay_var.get()),
                title=Path(self.video_path).stem)
            self.session.start()
            self.leave_btn.state(["!disabled"])
            self._save_config()
            dlg.destroy()

        ttk.Button(frm, text="Start hosting", command=start).grid(
            row=5, column=1, sticky="e", pady=(12, 0))
        dlg.grab_set()

    def _join_dialog(self):
        if self._session_running():
            messagebox.showinfo("StreamSync", "Leave the current session first.")
            return
        if not self.video_path:
            messagebox.showinfo("StreamSync", "Choose your copy of the film first.")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Join a session")
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=12)
        frm.grid()

        ttk.Label(frm, text="Relay server").grid(row=0, column=0, sticky="w")
        relay_var = tk.StringVar(value=self.relay_url)
        ttk.Entry(frm, textvariable=relay_var, width=34).grid(
            row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(frm, text="Session code").grid(row=1, column=0, sticky="w",
                                                 pady=(6, 0))
        code_var = tk.StringVar()
        ttk.Entry(frm, textvariable=code_var, width=14).grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Label(frm, text="Password").grid(row=2, column=0, sticky="w",
                                             pady=(6, 0))
        pw_var = tk.StringVar()
        ttk.Entry(frm, textvariable=pw_var, width=18).grid(
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
            self.leave_btn.state(["!disabled"])
            self._save_config()
            dlg.destroy()

        ttk.Button(frm, text="Join", command=start).grid(
            row=3, column=1, sticky="e", pady=(12, 0))
        dlg.grab_set()

    def _leave_session(self):
        if self.session is not None:
            # Closing the websocket runs a handshake that can sit for
            # seconds when the relay is unreachable - which is exactly when
            # someone reaches for Leave. The main thread drives the UI, so
            # it cannot wait for that.
            sess, self.session = self.session, None
            threading.Thread(target=sess.stop, daemon=True).start()
        self.leave_btn.state(["disabled"])
        self.session_lbl.config(text="Left the session.")

    # ------------------------------------------- stream window swap (facecam)

    def _refresh_windows(self):
        wins = [(h, t) for h, t in windowctl.list_windows()
                if t.strip() and not t.startswith("StreamSync")]
        self._win_map = {t[:70]: h for h, t in wins}
        items = list(self._win_map.keys())
        self.streamwin_combo["values"] = items
        if not self.streamwin_var.get():
            for t in items:
                if "twitch" in t.lower() or "kick.com" in t.lower():
                    self.streamwin_var.set(t)
                    self._on_streamwin_pick()
                    break

    def _on_streamwin_pick(self, _event=None):
        title = self.streamwin_var.get()
        self.stream_hwnd = self._win_map.get(title)
        self.stream_title = title
        self._save_config()

    def _stream_swap(self, show):
        """Show the stream's browser window during pauses; hide it again after."""
        if not self.swap_var.get() or show == self._swapped or not self.video_path:
            return
        log.debug("stream_swap: show_stream=%s (film window %s)", show,
                  "hides" if show else "returns")
        try:
            if show:
                hwnd = windowctl.find_stream_window(self.stream_hwnd,
                                                    self.stream_title)
                if hwnd is None:
                    self._set_status("Couldn't find the stream window - pick it "
                                     "under 'Stream window' (hit Refresh).")
                    return
                self.stream_hwnd = hwnd
                if self.player is self.embedded:
                    self._was_fullscreen = self.fullscreen
                    # drop the borderless frame before hiding, or the window
                    # comes back still stretched over the monitor
                    self._set_fullscreen(False, show=False)
                    self.video_win.withdraw()
                elif self.external is not None and self.external.proc:
                    self._ext_hwnd = windowctl.find_by_pid(self.external.proc.pid)
                    if self._ext_hwnd:
                        windowctl.minimize(self._ext_hwnd)
                windowctl.restore(hwnd)
                self._swapped = True
            else:
                if self.stream_hwnd and windowctl.is_valid(self.stream_hwnd):
                    windowctl.minimize(self.stream_hwnd)
                if self.player is self.embedded:
                    if self._was_fullscreen:
                        self._set_fullscreen(True)
                    else:
                        self.video_win.deiconify()
                elif self._ext_hwnd and windowctl.is_valid(self._ext_hwnd):
                    windowctl.restore(self._ext_hwnd)
                self._swapped = False
        except Exception as e:
            self._set_status(f"Window swap failed: {e}")

    def _on_mute_toggle(self):
        self.player.set_mute(self.mute_var.get())

    def _fullscreen_clicked(self):
        if self.player is self.embedded:
            self._set_fullscreen(not self.fullscreen)
        else:
            self.external.fullscreen_toggle()

    def _set_fullscreen(self, flag, show=True):
        if show:
            self.video_win.deiconify()
        if flag == self.fullscreen:
            return
        # Own the OS frame rather than using Tk's -fullscreen, which always
        # picks the primary display no matter which monitor the film is on.
        hwnd = self._video_hwnd()
        try:
            if flag:
                self._fs_saved = windowctl.borderless_fullscreen(hwnd)
                log.debug("fullscreen on monitor %s",
                          windowctl.monitor_rect(hwnd))
            elif self._fs_saved is not None:
                windowctl.unfullscreen(hwnd, self._fs_saved)
                self._fs_saved = None
        except Exception as e:
            log.warning("fullscreen switch failed, falling back to Tk: %s", e)
            self.video_win.attributes("-fullscreen", flag)
        self.fullscreen = flag

    def _video_hwnd(self):
        """The video window's real OS frame (winfo_id is Tk's inner child)."""
        return int(self.video_win.frame(), 16)

    # ------------------------------------------- video window mouse controls

    def _build_video_menu(self):
        m = tk.Menu(self.video_win, tearoff=0)
        m.add_command(label="Play/Pause", accelerator="Space",
                      command=self._toggle_pause)
        m.add_command(label="Fullscreen", accelerator="F11",
                      command=self._fullscreen_clicked)
        m.add_separator()
        m.add_checkbutton(label="Mute local audio", variable=self.mute_var,
                          command=self._on_mute_toggle)
        m.add_command(label="Volume up", accelerator="Wheel",
                      command=lambda: self._step_volume(+5))
        m.add_command(label="Volume down",
                      command=lambda: self._step_volume(-5))
        m.add_separator()
        self._sub_menu = tk.Menu(m, tearoff=0)
        m.add_cascade(label="Subtitles", menu=self._sub_menu)
        m.add_separator()
        m.add_command(label="Hide video window",
                      command=self.video_win.withdraw)
        self.video_menu = m

    def _show_video_menu(self, event):
        if self.player is not self.embedded:
            return
        x, y = event.x_root, event.y_root
        self.video_win.after(1, self._popup_video_menu, x, y)

    def _popup_video_menu(self, x, y):
        if self._closing:
            return          # a right-click can race the app closing
        self._rebuild_sub_menu()
        try:
            self.video_menu.tk_popup(x, y)
        finally:
            self.video_menu.grab_release()

    def _rebuild_sub_menu(self):
        m = self._sub_menu
        m.delete(0, "end")
        current = self.embedded.current_subtitle()
        tracks = self.embedded.subtitle_tracks()
        self._sub_pick = tk.IntVar(value=current)
        if not tracks:
            m.add_command(label="(none listed - start playback first)",
                          state="disabled")
        for tid, name in tracks:
            m.add_radiobutton(label=name, value=tid, variable=self._sub_pick,
                              command=lambda t=tid: self.embedded.set_subtitle(t))
        m.add_separator()
        m.add_command(label="Load subtitle file...",
                      command=self._load_sub_file)

    def _step_volume(self, step):
        v = self.embedded.volume()
        v = 100 if v is None else v
        v = max(0, min(125, v + step))
        self.embedded.set_volume(v)
        # never unmutes: "Mute local audio" is load-bearing while streaming
        # (loopback must not hear the film), so an accidental scroll must
        # not defeat it the way stock VLC's wheel would.
        self._flash_osd(f"Volume {v}%" + (" (muted)" if self.mute_var.get()
                                          else ""))

    def _flash_osd(self, text, ms=900):
        """VLC-style OSD drawn by Tk, floated over the video's top-right.
        (libvlc's own marquee cannot be driven from the thread hosting the
        video window - see the note in players.py.)"""
        if self._osd_win is None:
            self._osd_win = tk.Toplevel(self.video_win)
            self._osd_win.overrideredirect(True)
            self._osd_win.attributes("-topmost", True)
            self._osd_lbl = tk.Label(self._osd_win, bg="#111111",
                                     fg="#e6e6e6", padx=12, pady=5,
                                     font=("Segoe UI", 13, "bold"))
            self._osd_lbl.pack()
            self._osd_win.withdraw()
        self._osd_lbl.config(text=text)
        self._osd_win.update_idletasks()
        x = (self.video_win.winfo_rootx() + self.video_win.winfo_width()
             - self._osd_win.winfo_reqwidth() - 18)
        y = self.video_win.winfo_rooty() + 18
        self._osd_win.geometry(f"+{x}+{y}")
        self._osd_win.deiconify()
        if self._osd_after is not None:
            self.video_win.after_cancel(self._osd_after)
        self._osd_after = self.video_win.after(ms, self._osd_win.withdraw)

    def _on_video_wheel(self, event):
        if self.player is not self.embedded:
            return
        self.video_win.after(1, self._step_volume,
                             5 if event.delta > 0 else -5)

    def _shield_video_input(self):
        """Make libvlc's vout child transparent to mouse hit-testing.

        The vout swallows every mouse event over the video - they are
        neither handled (mouse input is disabled) nor forwarded - which
        would leave the wheel/double-click/menu bindings dead exactly
        where the user aims. WS_EX_TRANSPARENT drops hits through to our
        frame. The vout is recreated on every load, so sweep once a
        second rather than trying to catch each creation."""
        try:
            GWL_EXSTYLE, WS_EX_TRANSPARENT = -20, 0x00000020
            u32 = ctypes.windll.user32
            fresh = []

            def cb(hwnd, _lp):
                st = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                if not st & WS_EX_TRANSPARENT:
                    u32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                       st | WS_EX_TRANSPARENT)
                    fresh.append(hex(hwnd or 0))
                return True

            proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                      ctypes.c_void_p)(cb)
            u32.EnumChildWindows(self.video_frame.winfo_id(), proc, 0)
            if fresh:
                log.debug("shielded new vout children: %s", fresh)
        except Exception:
            pass
        # 250ms, not 1s: a fresh vout must not be double-clickable before
        # it is marked - VLC's own dblclick-fullscreen detaches the video
        # into a top-level window the embed never recovers from.
        self.video_win.after(250, self._shield_video_input)

    # ------------------------------------------------------------- subtitles

    def _refresh_subs(self):
        if self.player is not self.embedded:
            self._set_status("Subtitle picker applies to the embedded player; "
                             "in external mode use VLC's Subtitle menu.")
            return
        tracks = self.embedded.subtitle_tracks()
        items = [f"{tid}: {name}" for tid, name in tracks]
        self.sub_combo["values"] = items
        if items:
            self._set_status(f"{len(items)} subtitle entries found.")
        else:
            self._set_status("No subtitle tracks listed yet - start playback "
                             "first, then hit Refresh.")

    def _on_sub_pick(self, _event=None):
        val = self.sub_var.get()
        if ":" in val:
            try:
                self.embedded.set_subtitle(int(val.split(":", 1)[0]))
            except (ValueError, VLCError):
                pass

    def _load_sub_file(self):
        if self.player is not self.embedded:
            self._set_status("Load subtitle files through VLC's own menu in "
                             "external mode.")
            return
        path = filedialog.askopenfilename(
            title="Choose a subtitle file",
            filetypes=[("Subtitles", "*.srt *.ass *.ssa *.sub *.vtt"),
                       ("All files", "*.*")])
        if path:
            self.embedded.add_subtitle_file(path)
            self.root.after(600, self._refresh_subs)

    # ------------------------------------------------------------- plumbing

    def _poll_queue(self):
        try:
            while True:
                kind, *payload = self.q.get_nowait()
                if kind in ("preview", "devices", "show"):
                    log.debug("queue: %s", kind)      # payloads too bulky
                else:
                    log.debug("queue: %s %r", kind, payload)
                if kind == "status":
                    self._set_status(payload[0])
                elif kind == "session":
                    self.session_lbl.config(text=payload[0])
                elif kind == "swap":
                    self._stream_swap(payload[0])
                elif kind == "devices":
                    self._apply_devices(payload[0])
                elif kind == "preview":
                    self._show_preview(payload[0])
                elif kind == "show":
                    for win in payload[0]:
                        win.deiconify()
                elif kind == "adone":
                    match_t, score, z = payload
                    msg = (f"Matched stream audio at {fmt_time(match_t)} "
                           f"(score {score:.2f}, peak z {z:.0f}).")
                    if score < audio_matcher.SCORE_OK or z < audio_matcher.Z_OK:
                        msg += (" Weak match - the commentary may be drowning "
                                "the film audio; try again in a louder scene "
                                "or use video sync.")
                    else:
                        msg += " Nudge if the picture leads/lags the voice track."
                    self._set_status(msg)
                elif kind == "vdone":
                    match_t, score = payload
                    msg = (f"Matched stream video at {fmt_time(match_t)} "
                           f"(confidence {score:.2f}).")
                    if score < LOW_CONFIDENCE:
                        msg += (" Low confidence - check the capture region, or "
                                "set a facecam ignore zone under Edge cases.")
                    self._set_status(msg)
                elif kind == "error":
                    self._set_status(f"Sync failed: {payload[0]}")
                elif kind == "busy_off":
                    self.busy = False
                    self.sync_btn.state(["!disabled"])
                    self.resync_btn.state(["!disabled"])
                elif kind == "hotkey":
                    self._hotkey(payload[0])
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    def _hotkey(self, name):
        if name == "sync":
            self._sync()
        elif name == "resync":
            self._resync()
        elif name == "pause":
            self._toggle_pause()
        elif name == "back":
            self._nudge(-0.1)
        elif name == "fwd":
            self._nudge(0.1)

    def _install_hotkeys(self):
        try:
            import keyboard
            keyboard.add_hotkey("ctrl+alt+s", lambda: self.q.put(("hotkey", "sync")))
            keyboard.add_hotkey("ctrl+alt+r", lambda: self.q.put(("hotkey", "resync")))
            keyboard.add_hotkey("ctrl+alt+p", lambda: self.q.put(("hotkey", "pause")))
            keyboard.add_hotkey("ctrl+alt+left", lambda: self.q.put(("hotkey", "back")))
            keyboard.add_hotkey("ctrl+alt+right", lambda: self.q.put(("hotkey", "fwd")))
            self.hotkey_lbl.config(
                text="Global hotkeys: Ctrl+Alt+S sync | Ctrl+Alt+R resync | "
                     "Ctrl+Alt+P pause | Ctrl+Alt+Left/Right nudge 0.1s")
        except Exception:
            self.hotkey_lbl.config(text="Global hotkeys unavailable "
                                        "(optional 'keyboard' package not working).")

    def _populate_audio_devices(self):
        def work():
            try:
                names = audio_capture.list_speakers()
                default = audio_capture.default_speaker_name()
            except Exception as e:
                self.q.put(("status", f"Could not list audio devices: {e}"))
                return
            self.q.put(("devices", [f"(default) {default}"] + names))
        threading.Thread(target=work, daemon=True).start()

    def _apply_devices(self, items):
        self.device_combo["values"] = items
        if not self.device_var.get():
            if self.audio_device:
                for n in items[1:]:
                    if self.audio_device.lower() in n.lower():
                        self.device_var.set(n)
                        break
            if not self.device_var.get():
                self.device_var.set(items[0])

    def _on_device_pick(self, _event=None):
        val = self.device_var.get()
        self.audio_device = "" if val.startswith("(default)") else val
        self._save_config()

    def _show_preview(self, gray_img):
        img = Image.fromarray((gray_img * 255).clip(0, 255).astype("uint8"))
        img.thumbnail((160, 90))
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
                "stream_title": self.stream_title,
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
            log.info("config auto-load into embedded (window hidden): %r",
                     path)
            self.embedded.load(path)
            self.file_lbl.config(text=Path(path).name)
        region = cfg.get("region")
        if region and len(region) == 4:
            self.region = tuple(int(v) for v in region)
            self.region_lbl.config(
                text=f"{self.region[2]}x{self.region[3]} at "
                     f"({self.region[0]}, {self.region[1]}) (from last session)")
        if cfg.get("window"):
            self.window_var.set(cfg["window"])
        if cfg.get("hint"):
            self.hint_var.set(cfg["hint"])
        if cfg.get("method") in ("audio", "video"):
            self.method_var.set(cfg["method"])
        self.audio_device = cfg.get("audio_device", "")
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
        self.stream_title = cfg.get("stream_title", "")
        if cfg.get("relay_url"):
            self.relay_url = cfg["relay_url"]
        if self.stream_title:
            self.streamwin_var.set(self.stream_title)
        # note: player choice is restored as "embedded"; external VLC is only
        # spawned when the user picks it, never on startup

    def _on_close(self):
        self._closing = True
        if self.session is not None:
            # Give the room teardown a moment to go out, but do not let a
            # dead relay hold the window on screen for its full close
            # timeout - quitting should feel immediate.
            closer = threading.Thread(target=self.session.stop, daemon=True)
            closer.start()
            closer.join(1.5)
        self._save_config()
        try:
            import keyboard
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            self.embedded.stop()
        except Exception:
            pass
        self.root.destroy()


def main():
    # make Tk report physical pixels so regions line up with mss captures
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    if "--selftest" in sys.argv:
        root.after(3000, root.destroy)
    root.mainloop()
    if "--selftest" in sys.argv:
        print("SELFTEST OK")


if __name__ == "__main__":
    main()
