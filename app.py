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
PROBE_MAX_AHEAD = 600.0    # cap the growing resume-search window (_auto_loop)
LOW_CONFIDENCE = 0.55      # video-match warning threshold


def fmt_time(s):
    # integer tenths, so rounding carries: formatting 119.96 by splitting
    # int(s) from a rounded fraction yields the impossible "1:60.0", and a
    # smooth 10 fps clock sweeps through that window at most minute marks
    tenths = int(round(max(0.0, float(s)) * 10))
    whole, frac = divmod(tenths, 10)
    h, rem = divmod(whole, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}.{frac}"
    return f"{m}:{sec:02d}.{frac}"




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
        self.auto_follow = False     # experimental; Alex wants default off
        self.auto_interval = 30
        self._last_sync = None       # context for the verdict buttons
        self._closing = False
        self._preview_photo = None
        self.external = None         # created on demand
        self.stream_hwnd = None      # pinned stream browser window
        self.stream_title = ""
        self._win_map = {}
        self._swapped = False        # stream window currently shown?
        self._stream_placement = None  # where the stream window belongs
        self._search_cancel = None   # Event; Stop sets it
        self._pending_search = None  # "resync" queued behind a cancel
        self.session = None          # active HostSession / ViewerSession
        self.relay_url = "ws://localhost:8765"
        self._osd_win = None         # Tk-drawn volume OSD over the video
        self._osd_after = None
        self._clock = None           # last player sample the clock shows
        self._clock_shown = None     # (player id, seconds) last painted
        self._film_fps = None        # probed per file; frame nudge unit
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
        threading.Thread(target=self._clock_sampler, daemon=True).start()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        # frozen builds have no console: Tk callback errors would vanish
        root.report_callback_exception = \
            lambda *exc: log.error("Tk callback error", exc_info=exc)
        root.after(80, self._poll_queue)
        root.after(100, self._render_time)
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
        # up here next to the file, not buried among the playback controls:
        # a film restored from config leaves the video window hidden, and
        # the only other thing that revealed it was re-picking the already
        # selected Player radio - which reads as "nothing happened".
        self.open_player_btn = ttk.Button(row, text="Open player",
                                          command=self._open_player)
        self.open_player_btn.pack(side="right")
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
        self.sync_btn = ttk.Button(row, text="Sync to stream",
                                   command=self._sync_clicked)
        self.sync_btn.pack(side="left")
        self.resync_btn = ttk.Button(row, text="Resync (around current position)",
                                     command=self._resync_clicked)
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
        self.follow_var = tk.BooleanVar(value=False)
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
                                         command=self._open_player)
        self.show_video_btn.pack(side="left", padx=(10, 0))

        row = ttk.Frame(play)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Nudge:").pack(side="left")
        # one frame is the precision unit; big drift is what Resync is for
        ttk.Button(row, text="-1 Frame", width=9,
                   command=lambda: self._nudge(-self._frame_s())
                   ).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="+1 Frame", width=9,
                   command=lambda: self._nudge(self._frame_s())
                   ).pack(side="left", padx=(4, 0))
        self.offset_lbl = ttk.Label(row, text="offset: +0.00s")
        self.offset_lbl.pack(side="left", padx=(10, 0))
        ttk.Button(row, text="Reset", width=6,
                   command=self._reset_offset).pack(side="left", padx=(4, 0))

        # monospace: with a proportional font the line shifts sideways as
        # digit widths change, which reads as jitter even at perfect cadence
        self.time_lbl = ttk.Label(outer, text="-:-- / -:--",
                                  font=("Consolas", 10))
        self.time_lbl.grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.status_lbl = ttk.Label(outer, text="Pick a video file to begin.",
                                    wraplength=620, foreground="#245")
        self.status_lbl.grid(row=5, column=0, sticky="w", pady=(4, 0))
        self.hotkey_lbl = ttk.Label(outer, text="", foreground="#666")
        self.hotkey_lbl.grid(row=6, column=0, sticky="w", pady=(4, 0))

        # ---------------- Experimental (right-side partition)
        ttk.Separator(outer, orient="vertical").grid(
            row=0, column=1, rowspan=7, sticky="ns", padx=10)
        exp = ttk.LabelFrame(outer, text="Experimental", padding=8)
        exp.grid(row=0, column=2, rowspan=7, sticky="new")
        ttk.Label(exp, text="Longer listens\n(more signal under\n"
                            "the commentary):",
                  justify="left").pack(anchor="w")
        self._exp_sync_btns = []
        for secs in (6, 15, 30):
            b = ttk.Button(exp, text=f"Sync ({secs} s listen)", width=18,
                           command=lambda s=secs: self._sync(listen_s=s))
            b.pack(anchor="w", pady=(4, 0))
            self._exp_sync_btns.append(b)
        ttk.Separator(exp, orient="horizontal").pack(fill="x", pady=10)
        ttk.Label(exp, text="Did that sync land\nwhere it should?",
                  justify="left").pack(anchor="w")
        vrow = ttk.Frame(exp)
        vrow.pack(anchor="w", pady=(4, 0))
        # plain tk.Buttons: ttk's vista theme ignores button colors
        self.good_btn = tk.Button(vrow, text="✓ Worked", width=8,
                                  bg="#2e7d32", fg="white",
                                  activebackground="#1b5e20",
                                  activeforeground="white", state="disabled",
                                  command=lambda: self._sync_verdict(True))
        self.good_btn.pack(side="left")
        self.bad_btn = tk.Button(vrow, text="✗ Off", width=8,
                                 bg="#c62828", fg="white",
                                 activebackground="#8e0000",
                                 activeforeground="white", state="disabled",
                                 command=lambda: self._sync_verdict(False))
        self.bad_btn.pack(side="left", padx=(6, 0))
        ttk.Label(exp, text="Every verdict is logged\nwith the match's score,\n"
                            "and tunes the confidence\nbar the matcher uses.",
                  foreground="#666", justify="left").pack(anchor="w",
                                                          pady=(6, 0))

    def _sync_verdict(self, ok):
        """Ground truth from the person watching: label the last sync so the
        log accumulates (score, z, listen length) -> worked/failed pairs.
        The app's own 'Matched' message is not evidence - it seeks to the
        best peak even when the peak is noise."""
        if self._last_sync is None:
            return
        log.info("SYNC VERDICT %s: %s", "WORKED" if ok else "FAILED",
                 self._last_sync)
        # both buttons stay pressable on purpose: pressing again corrects
        # the label ("worked"... then the drift shows). Per sync, the LAST
        # verdict line in the log wins; starting a new search disarms both.
        self._set_status("Logged: sync " + ("worked." if ok else "was off."))

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
            self._clock_set_playing(False)
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
        self._film_fps = None
        threading.Thread(target=self._probe_fps_worker, args=(path,),
                         daemon=True).start()
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

    def _open_player(self):
        """Put the current player on screen with a picture in it.

        Deiconifying alone leaves a black rectangle: a film loaded from
        config has media but has never played, so libvlc has no video
        output at all. Start it and settle on a frame - muted by default,
        so nothing is audible and nothing runs away.
        """
        if not self.video_path:
            messagebox.showinfo("StreamSync", "Choose the film file first.")
            return
        if self.player is not self.embedded:
            hwnd = (windowctl.find_by_pid(self.external.proc.pid)
                    if self.external is not None and self.external.proc
                    else None)
            if hwnd:
                windowctl.restore(hwnd)
            else:
                self._apply_player_choice()     # spawn it
            return
        self.video_win.deiconify()
        self.video_win.lift()
        self.video_win.focus_set()
        # Only prime a film that has never been on screen. has_vout() is the
        # honest test: a film paused at a position the user already synced
        # has one, and seeking that back to a preview frame would throw
        # their sync away.
        if (self.embedded.mp.has_vout() or self.busy
                or self._session_running()):
            return
        log.info("open player: priming a preview frame")
        self.embedded.mp.play()
        self.root.after(120, self._prime_preview, 0, None)

    def _prime_preview(self, tries, target_ms):
        """Show a frame with something in it, then pause. Never blocks the UI.

        Films open on black - studio idents fade up from it - so pausing on
        frame one looks exactly like the failure this button exists to fix.
        Settle a little way in instead; the playhead is meaningless until a
        sync anyway.
        """
        if self._closing or self.busy or self._session_running():
            return
        mp = self.embedded.mp
        if tries >= 60:                          # ~6s, then give up quietly
            log.warning("open player: no preview frame (vouts=%s t=%s)",
                        mp.has_vout(), mp.get_time())
            return
        if not (mp.has_vout() and (mp.get_time() or 0) > 0):
            self.root.after(100, self._prime_preview, tries + 1, target_ms)
            return
        if target_ms is None:                    # decoding has begun: aim
            length = mp.get_length() or 0
            target_ms = int(min(max(length * 0.08, 30_000), 600_000)
                            if length > 0 else 60_000)
            mp.set_time(target_ms)
            self.root.after(150, self._prime_preview, tries + 1, target_ms)
            return
        if (mp.get_time() or 0) >= target_ms - 2_000:
            mp.set_pause(1)
            self._clock_set_playing(False)
            self._set_status(f"{Path(self.video_path).name} ready - paused at "
                             f"{fmt_time(target_ms / 1000.0)}. Sync or Resync "
                             "to line it up with the stream.")
            return
        self.root.after(100, self._prime_preview, tries + 1, target_ms)

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

    def _sync(self, listen_s=None):
        if self.busy:
            return
        method = self.method_var.get()
        if not self._ready(need_region=(method == "video")):
            return
        # whole-file, always: the hint field is gone - field data showed
        # unhinted search finding the spot better than expected
        self._start_search(None, None, listen_s)

    def _sync_clicked(self):
        """The Sync button doubles as Stop while a search is running."""
        if self.busy:
            self._stop_search()
        else:
            self._sync()

    def _resync_clicked(self):
        """Resync during a search cancels it and starts over."""
        if self.busy:
            self._stop_search()
            self._pending_search = "resync"
        else:
            self._resync()

    def _stop_search(self):
        if self.busy and self._search_cancel is not None:
            # Stop means stop: a Resync queued behind the cancel must not
            # auto-start a new search against an explicit abort. (Resync's
            # own click re-queues AFTER calling this, so restart survives.)
            self._pending_search = None
            self._search_cancel.set()
            self._set_status("Stopping...")

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
        # a paused stream leaves the local copy ahead, so look mostly
        # backwards; ten minutes covers any realistic drift or pause.
        # `near` breaks ties by proximity: ten minutes of a self-similar
        # film can easily hold a twin of where we actually are.
        self._start_search(t - 600.0, t + 30.0, near=t)

    def _start_search(self, a, b, listen_s=None, near=None):
        self.busy = True
        self._search_cancel = threading.Event()
        # Sync morphs into Stop; Resync stays live so a press mid-search
        # cancels and starts over. Only the experimental buttons lock.
        self.sync_btn.config(text="Stop")
        for btn in self._exp_sync_btns:
            btn.state(["disabled"])
        # a new search invalidates the previous verdict context: without
        # this, a failed attempt leaves the buttons armed with the LAST
        # sync's numbers, and a "that failed" press would mislabel a sync
        # that actually worked - poisoning the dataset these exist for
        self.good_btn.config(state="disabled")
        self.bad_btn.config(state="disabled")
        self._last_sync = None
        player = self.player
        pname = type(player).__name__   # the player the worker will seek -
        offset = self.offset            # active_player may change mid-listen
        mute = self.mute_var.get()
        if self.method_var.get() == "audio":
            listen = float(listen_s or AUDIO_SYNC_SECONDS)
            log.info("sync search: window=%s..%s listen=%.0fs player=%s",
                     a, b, listen, pname)
            device = self.audio_device
            threading.Thread(
                target=self._audio_search_worker,
                args=(a, b, player, offset, mute, device, listen, pname,
                      self._search_cancel, near),
                daemon=True).start()
        else:
            self._set_status("Capturing stream frames...")
            hidden = self._hide_overlapping_windows()
            mask = self._build_mask()
            mirror = self.mirror_var.get()
            threading.Thread(
                target=self._video_search_worker,
                args=(a, b, player, offset, mute, hidden, mask, mirror, pname,
                      self._search_cancel),
                daemon=True).start()

    # ------------------------------------------------------------- workers

    def _audio_search_worker(self, a, b, player, offset, mute, device, listen,
                             pname, cancel, near):
        try:
            self.q.put(("status",
                        f"Recording stream audio ({listen:.0f} s)..."))
            samples, sr, t0 = audio_capture.record_loopback(
                listen, speaker_name=device, cancel=cancel.is_set)
            feats = audio_matcher.prep_capture(samples, sr)
            m = audio_matcher.find_match_audio_ex(
                self.video_path, feats, a, b,
                progress=lambda msg: self.q.put(("status", msg)),
                cancel=cancel.is_set, near=near)
            if cancel.is_set():
                raise RuntimeError("stopped")
            self.q.put(("status", "Seeking..."))
            player.sync_seek(m.t, t0, offset)
            player.set_mute(mute)
            self.q.put(("swap", False))
            self.q.put(("adone", m.t, m.score, m.z, listen, pname,
                        m.ambiguous, m.rival_t, m.rival_score))
        except Exception as e:
            # a stop is not a failure - report it as what it was
            if cancel.is_set():
                self.q.put(("cancelled", None))
            else:
                self.q.put(("error", str(e)))
        finally:
            self.q.put(("busy_off", None))

    def _video_search_worker(self, a, b, player, offset, mute, hidden,
                             mask, mirror, pname, cancel):
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
            if cancel.is_set():
                raise RuntimeError("stopped")
            # NOTE: the frame scan itself is not yet interruptible - Stop
            # takes effect before it starts and before the seek lands
            match_t, score = matcher.find_match(
                self.video_path, burst, a, b,
                progress=lambda m: self.q.put(("status", m)), mask=mask)
            if cancel.is_set():
                raise RuntimeError("stopped")
            self.q.put(("status", "Seeking..."))
            player.sync_seek(match_t, t0, offset)
            player.set_mute(mute)
            self.q.put(("swap", False))
            self.q.put(("vdone", match_t, score, pname))
        except Exception as e:
            self.q.put(("show", hidden))
            if cancel.is_set():
                self.q.put(("cancelled", None))
            else:
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

    def _auto_probe(self, ring, lo, hi, near):
        """Match `ring` (list of (block, t0) pairs) inside [lo, hi].

        Returns (t, score, z, ring_t0) on a gate-passing hit, else None.
        `t` is where the stream was at the RING's start, so seeks pair it
        with ring_t0 exactly like a one-shot capture's t0. Silence counts
        as no-match: a silent stream is a paused stream as far as syncing
        is concerned.
        """
        try:
            samples = np.concatenate([b for b, _ in ring])
            feats = audio_matcher.prep_capture(samples, audio_capture.CAPTURE_SR)
            # `near` must be a position we actually believe in, passed by
            # the caller. Deriving it from the window would drift with the
            # window: probe mode's forward edge grows for minutes, and its
            # midpoint slides away from where a resume really happens.
            m = audio_matcher.find_match_audio_ex(
                self.video_path, feats, lo, hi, near=near)
        except (RuntimeError, ValueError, matcher.MatchError):
            return None
        if (m.z >= audio_matcher.Z_OK and m.score >= audio_matcher.SCORE_OK
                and not m.ambiguous):
            return m.t, m.score, m.z, ring[0][1]
        return None

    def _auto_loop(self):
        """Live follow: a held-open listener, an energy tripwire, and
        tight-window match confirms.

        The stream's audio is read in one-second blocks into a short ring.
        Normal mode confirms the match on a cadence AND immediately when
        the block energy collapses below the rolling baseline - the film
        audio vanishing is what a pause sounds like - so a real pause is
        caught in a couple of seconds instead of an interval. A false
        pause self-heals: probe mode keeps listening at the same live
        cadence and resyncs the moment the film's audio reappears.
        Corrections require two consecutive agreeing drift readings; a
        single reading can be one bad match.
        """
        listener = None
        listen_dev = None
        mode = "normal"
        ring = []            # [(block, t0_perf)] newest last, ~2.5s total
        ring_len = 0.0
        baseline = None      # EMA of block RMS while matching confirms
        tripwire = True      # energy trigger armed; a miss disarms it so a
                             # long quiet stretch cannot storm probes at 1 Hz
        pending_drift = None
        misses = 0
        pause_point = None
        paused_at = 0.0
        last_confirm = 0.0
        BLOCK_S = 1.0
        RING_S = 2.5

        def close_listener():
            nonlocal listener, ring, ring_len, baseline
            if listener is not None:
                listener.close()
            listener = None
            ring, ring_len, baseline = [], 0.0, None

        while not self._closing:
            if (not self.auto_enabled or self.busy or not self.video_path
                    or self._session_running()):  # sessions own the playhead
                mode, misses, pending_drift = "normal", 0, None
                close_listener()
                time.sleep(1.0)
                continue
            if listener is None or listen_dev != self.audio_device:
                try:
                    close_listener()
                    listener = audio_capture.Listener(
                        self.audio_device or None)
                    listen_dev = self.audio_device
                except Exception as e:
                    self.q.put(("status", f"Auto listener failed: {e}"))
                    listener = None
                    time.sleep(5.0)
                    continue
            try:
                block, t0_blk = listener.read(BLOCK_S)
            except Exception as e:
                self.q.put(("status", f"Auto listener failed: {e}"))
                close_listener()
                time.sleep(3.0)
                continue
            ring.append((block, t0_blk))
            ring_len += BLOCK_S
            while ring_len - BLOCK_S >= RING_S:
                ring.pop(0)
                ring_len -= BLOCK_S
            rms = float(np.sqrt(np.mean(block * block)))
            if ring_len < RING_S:
                continue
            player = self.active_player
            try:
                if mode == "normal":
                    energy_drop = (baseline is not None and tripwire
                                   and rms < 0.35 * baseline
                                   and time.monotonic() - last_confirm >= 2.0)
                    cadence = min(max(float(self.auto_interval), 4.0), 30.0)
                    if pending_drift is not None:
                        cadence = 2.5     # confirm a suspected drift fast
                    due = time.monotonic() - last_confirm >= cadence
                    if not (energy_drop or due):
                        continue
                    t_ref = player.time() if player.is_playing() else None
                    t_ref_at = time.perf_counter()
                    if t_ref is None:
                        last_confirm = time.monotonic()
                        continue
                    c = t_ref - self.offset   # expected STREAM position
                    hit = self._auto_probe(ring, c - 30.0, c + 30.0, near=c)
                    last_confirm = time.monotonic()
                    if hit:
                        t, score, z, t0 = hit
                        misses = 0
                        tripwire = True
                        baseline = (rms if baseline is None
                                    else 0.9 * baseline + 0.1 * rms)
                        # measure the film at the RING's start: t_ref
                        # predates it and the match ran for a while after
                        now_pos = player.time()
                        moved = None if now_pos is None else now_pos - t_ref
                        if (now_pos is None or not player.is_playing()
                                or abs(moved - (time.perf_counter() - t_ref_at))
                                > 0.5):
                            # paused, seeked or stalled while we listened -
                            # winding the clock back would invent a position
                            pending_drift = None
                            continue
                        film_at_t0 = now_pos - (time.perf_counter() - t0)
                        # the stream's position plus the offset the user
                        # nudged in, which every sync_seek applies -
                        # ignoring it reads a deliberate offset as drift
                        drift = ((t + self.offset)
                                 - (film_at_t0 + self._clock_lag()))
                        if abs(drift) <= 0.35:
                            pending_drift = None
                        elif pending_drift is None:
                            pending_drift = drift   # once could be a fluke
                        elif abs(drift - pending_drift) < 0.4 \
                                and not self.busy \
                                and not self._session_running():
                            player.sync_seek(t, t0, self.offset)
                            self.q.put(("status",
                                        f"Auto: corrected {drift:+.2f}s drift "
                                        f"(score {score:.2f}, z {z:.0f}, "
                                        "confirmed twice)."))
                            pending_drift = None
                        else:
                            pending_drift = drift
                    else:
                        misses += 1
                        pending_drift = None
                        tripwire = False   # re-arms on the next hit
                        # a miss plus the energy collapse IS the pause
                        # signature; without the energy cue ask twice
                        if self.auto_follow and (
                                misses >= 2 or (misses >= 1 and energy_drop)):
                            player.pause()
                            pause_point = c
                            paused_at = time.monotonic()
                            mode = "probe"
                            self.q.put(("swap", True))
                            self.q.put(("status",
                                        "Auto: film audio not found on the stream "
                                        "- assuming pause. Watching for resume..."))
                else:  # probe: film paused, live watch for the resume
                    if time.monotonic() - last_confirm < 2.0:
                        continue
                    # a real pause resumes near pause_point; a FALSE pause
                    # means the stream never stopped, so the forward edge
                    # grows with elapsed time to keep up with it - that is
                    # also what makes a wrong pause heal itself
                    ahead = min(time.monotonic() - paused_at, PROBE_MAX_AHEAD)
                    # near is pause_point, NOT the window's middle: the
                    # window grows forward to catch a stream that never
                    # stopped, but a real resume happens where it paused
                    hit = self._auto_probe(ring, pause_point - 25.0,
                                           pause_point + 40.0 + ahead,
                                           near=pause_point)
                    last_confirm = time.monotonic()
                    if hit:
                        t, score, z, t0 = hit
                        if self.busy or self._session_running():
                            # someone else is driving the playhead; leaving
                            # probe mode now would strand a paused film
                            continue
                        player.sync_seek(t, t0, self.offset)
                        self.q.put(("swap", False))
                        self.q.put(("status", "Auto: stream resumed - resynced."))
                        mode, misses, baseline = "normal", 0, None
            except Exception as e:
                self.q.put(("status", f"Auto-resync check failed: {e}"))
                log.warning("auto loop error", exc_info=True)
                time.sleep(max(self.auto_interval, 10))
        close_listener()

    # ------------------------------------------------------------- playback

    def _frame_s(self):
        """One frame of the loaded film in seconds (23.976 until probed)."""
        return 1.0 / (self._film_fps or 23.976)

    def _probe_fps_worker(self, path):
        try:
            fps = matcher.probe_fps(path)
        except Exception:
            fps = None
        if path != self.video_path:
            return   # a newer film was chosen while this probe ran
        self._film_fps = fps
        log.info("film fps: %s (frame = %.1f ms)", fps,
                 1000.0 / (fps or 23.976))

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
        self._clock_set_playing(not was_playing)
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

    BROWSER_SUFFIXES = (" - brave", " - google chrome", " - chromium",
                        " - microsoft edge", " - mozilla firefox", " - opera")

    def _refresh_windows(self):
        wins = [(h, t) for h, t in windowctl.list_windows()
                if t.strip() and not t.startswith("StreamSync")]
        self._win_map = {t[:70]: h for h, t in wins}
        items = list(self._win_map.keys())
        self.streamwin_combo["values"] = items

        current = self.streamwin_var.get()
        if current in self._win_map:
            self.stream_hwnd = self._win_map[current]  # re-resolve each refresh
            return
        if current:
            # A title saved last session whose window is gone would sit in
            # the box forever: it blocks the auto-pick below and reads as
            # "a window is selected" when nothing is.
            log.info("saved stream window %r is gone - re-picking", current)
            self.streamwin_var.set("")
            self.stream_hwnd = None

        # A live stream's tab is titled "... - Twitch", but a VOD's tab is
        # titled after the VOD, so name matching alone finds nothing. Fall
        # back to the browser window itself when there is only one.
        for t in items:
            low = t.lower()
            if "twitch" in low or "kick.com" in low:
                self.streamwin_var.set(t)
                self._on_streamwin_pick()
                return
        browsers = [t for t in items
                    if any(t.lower().endswith(s)
                           for s in self.BROWSER_SUFFIXES)]
        if len(browsers) == 1:
            self.streamwin_var.set(browsers[0])
            self._on_streamwin_pick()

    def _on_streamwin_pick(self, _event=None):
        title = self.streamwin_var.get()
        self.stream_hwnd = self._win_map.get(title)
        self.stream_title = title
        self._save_config()

    def _stream_swap(self, show):
        """Fullscreen handoff during stream pauses.

        Only when the film is FULLSCREEN does the stream take its place -
        same monitor - and on resume the film takes the spot back while
        the stream window returns to exactly where it was. A windowed film
        means both windows are already arranged the way the user wants
        them (two monitors, both visible): touch nothing, and never, ever
        minimize the stream. External VLC manages its own windows.
        """
        if not self.swap_var.get() or show == self._swapped or not self.video_path:
            return
        log.debug("stream_swap: show_stream=%s fullscreen=%s", show,
                  self.fullscreen)
        try:
            if show:
                # only a FULLSCREEN EMBEDDED film swaps; the release path
                # below must stay reachable for other players, or a swap
                # engaged before switching to external wedges forever
                if self.player is not self.embedded or not self.fullscreen:
                    return
                hwnd = windowctl.find_stream_window(self.stream_hwnd,
                                                    self.stream_title)
                if hwnd is None:
                    self._set_status("Couldn't find the stream window - pick it "
                                     "under 'Stream window' (hit Refresh).")
                    return
                self.stream_hwnd = hwnd
                monitor = windowctl.monitor_rect(self._video_hwnd())
                self._stream_placement = windowctl.get_placement(hwnd)
                # drop the borderless frame before hiding, or the window
                # comes back still stretched over the monitor
                self._set_fullscreen(False, show=False)
                self.video_win.withdraw()
                windowctl.fill_monitor(hwnd, monitor)
                self._swapped = True
            else:
                if (self.stream_hwnd and windowctl.is_valid(self.stream_hwnd)
                        and self._stream_placement is not None):
                    windowctl.set_placement(self.stream_hwnd,
                                            self._stream_placement)
                self._stream_placement = None
                if self.player is self.embedded:
                    self._set_fullscreen(True)   # swap only engages fullscreen
                # else: the user switched to external mid-swap - give the
                # stream window back but leave the embedded window hidden
                self._swapped = False
        except Exception as e:
            self._set_status(f"Window swap failed: {e}")

    def _reveal_player(self, pname):
        """A manual sync just landed - put the synced player on screen.

        Restores what the user had (a withdrawn-while-fullscreen window
        keeps its borderless frame, so deiconify alone brings fullscreen
        back) and never steals keyboard focus - they may be typing in
        chat. Auto re-sync corrections never come through here.
        """
        try:
            if pname == "EmbeddedPlayer":
                if not self.video_win.winfo_viewable():
                    self.video_win.deiconify()
                self.video_win.lift()
            elif self.external is not None and self.external.proc:
                hwnd = windowctl.find_by_pid(self.external.proc.pid)
                if hwnd and windowctl.is_minimized(hwnd):
                    windowctl.show_noactivate(hwnd)
        except Exception as e:
            log.warning("reveal after sync failed: %s", e)

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
                if kind == "clock":
                    pass          # one per second forever; not worth logging
                elif kind in ("preview", "devices", "show"):
                    log.debug("queue: %s", kind)      # payloads too bulky
                else:
                    log.debug("queue: %s %r", kind, payload)
                if kind == "clock":
                    self._update_clock(*payload)
                elif kind == "status":
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
                    (match_t, score, z, listen, pname,
                     ambiguous, rival_t, rival_score) = payload
                    self._last_sync = {
                        "kind": "audio", "t": round(match_t, 3),
                        "score": round(score, 4), "z": round(z, 2),
                        "listen_s": listen, "player": pname,
                        "ambiguous": ambiguous,
                        "rival_t": None if rival_t is None else round(rival_t, 1),
                        "rival_score": round(rival_score, 4)}
                    self.good_btn.config(state="normal")
                    self.bad_btn.config(state="normal")
                    self._reveal_player(pname)
                    msg = (f"Matched stream audio at {fmt_time(match_t)} "
                           f"(score {score:.2f}, peak z {z:.0f}, "
                           f"{listen:.0f}s listen).")
                    if ambiguous:
                        # z cannot see this: it measures the peak against the
                        # curve's average, not against an equally good rival
                        msg += (f" AMBIGUOUS - {fmt_time(rival_t)} sounds "
                                f"almost the same ({rival_score:.2f} vs "
                                f"{score:.2f}). If this landed wrong, scrub "
                                "near the right moment and hit Resync, which "
                                "prefers the nearest match.")
                    elif score < audio_matcher.SCORE_OK or z < audio_matcher.Z_OK:
                        msg += (" Weak match - the commentary may be drowning "
                                "the film audio; try again in a louder scene "
                                "or use video sync.")
                    else:
                        msg += " Nudge if the picture leads/lags the voice track."
                    self._set_status(msg)
                elif kind == "vdone":
                    match_t, score, pname = payload
                    self._last_sync = {
                        "kind": "video", "t": round(match_t, 3),
                        "score": round(score, 4), "player": pname}
                    self.good_btn.config(state="normal")
                    self.bad_btn.config(state="normal")
                    self._reveal_player(pname)
                    msg = (f"Matched stream video at {fmt_time(match_t)} "
                           f"(confidence {score:.2f}).")
                    if score < LOW_CONFIDENCE:
                        msg += (" Low confidence - check the capture region, or "
                                "set a facecam ignore zone under Edge cases.")
                    self._set_status(msg)
                elif kind == "error":
                    self._set_status(f"Sync failed: {payload[0]}")
                elif kind == "cancelled":
                    self._set_status("Sync stopped.")
                elif kind == "busy_off":
                    self.busy = False
                    self._search_cancel = None
                    self.sync_btn.config(text="Sync to stream")
                    for btn in self._exp_sync_btns:
                        btn.state(["!disabled"])
                    pending, self._pending_search = self._pending_search, None
                    if pending == "resync" and not self._closing:
                        self.root.after(50, self._resync)
                elif kind == "hotkey":
                    self._hotkey(payload[0])
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    def _hotkey(self, name):
        if name == "sync":
            self._sync_clicked()
        elif name == "resync":
            self._resync_clicked()
        elif name == "pause":
            self._toggle_pause()
        elif name == "back":
            self._nudge(-self._frame_s())
        elif name == "fwd":
            self._nudge(self._frame_s())

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
                     "Ctrl+Alt+P pause | Ctrl+Alt+Left/Right nudge 1 frame")
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

    # ------------------------------------------------------------- clock
    #
    # The old clock polled the player three times per 700ms tick ON the Tk
    # thread - in external mode that is three HTTP round trips, each able
    # to stall the UI - and painted tenths of a second at a cadence that
    # steps the tenths digit by ~0.7. Chunky, and irregular by design.
    # Now: a worker samples the player once a second, and the Tk thread
    # extrapolates between samples with perf_counter, painting at 10 fps.

    def _clock_sampler(self):
        while not self._closing:
            player = self.active_player
            try:
                t, n, playing = player.snapshot()
            except Exception:
                t, n, playing = None, None, False
            self.q.put(("clock", t, n, playing, id(player),
                        time.perf_counter()))
            time.sleep(1.0)

    def _clock_set_playing(self, playing):
        """Tk-thread hint that playback state just changed.

        The sampler confirms within a second, but until then a paused
        player would keep 'ticking' on extrapolation and then visibly
        rewind when the real sample lands. Pauses the app cannot see
        coming (external VLC's own UI, the auto loop's thread) still get
        one small correction at the next sample - that one is honest.
        """
        c = self._clock
        if c is None or c["playing"] == playing:
            return
        t = c["t"]
        if c["playing"]:
            t += time.perf_counter() - c["perf"]   # freeze at the estimate
        self._clock = {**c, "t": t, "perf": time.perf_counter(),
                       "playing": playing}

    def _update_clock(self, t, n, playing, pid, perf):
        if t is None:
            self._clock = None
            return
        c = self._clock
        if c and playing and c["playing"] and c["pid"] == pid:
            expected = c["t"] + (perf - c["perf"])
            err = t - expected
            if abs(err) <= 0.75:
                # sampling jitter, not a seek: absorb it instead of jumping
                t = expected + 0.25 * err
        self._clock = {"t": t, "perf": perf, "len": n,
                       "playing": playing, "pid": pid}

    def _render_time(self):
        c = self._clock
        if c is not None:
            t = c["t"]
            if c["playing"]:
                t += time.perf_counter() - c["perf"]
                last = self._clock_shown
                if (last is not None and last[0] == c["pid"]
                        and t < last[1] and last[1] - t < 0.5):
                    t = last[1]     # never tick backwards over jitter
                self._clock_shown = (c["pid"], t)
            else:
                self._clock_shown = None
            total = f" / {fmt_time(c['len'])}" if c["len"] else ""
            state = "playing" if c["playing"] else "paused"
            self.time_lbl.config(text=f"{fmt_time(t)}{total}  ({state})")
        self.root.after(100, self._render_time)

    def _save_config(self):
        try:
            CONFIG_PATH.write_text(json.dumps({
                "video_path": self.video_path,
                "region": self.region,
                "method": self.method_var.get(),
                "player": self.player_var.get(),
                "audio_device": self.audio_device,
                "facecam": self.facecam_var.get(),
                "facecam_rect": self.facecam_rect,
                "mirror": self.mirror_var.get(),
                "auto_interval": self.auto_interval,
                "follow_pauses": self.follow_var.get(),
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
            threading.Thread(target=self._probe_fps_worker, args=(path,),
                             daemon=True).start()
            self.embedded.load(path)
            self.file_lbl.config(text=Path(path).name)
            # picking a file says so; restoring the same file said nothing,
            # so "Pick a video file to begin." sat there under a named film
            self._set_status(f"{Path(path).name} loaded from last session. "
                             "Open player to see it, then Sync to the stream.")
        region = cfg.get("region")
        if region and len(region) == 4:
            self.region = tuple(int(v) for v in region)
            self.region_lbl.config(
                text=f"{self.region[2]}x{self.region[3]} at "
                     f"({self.region[0]}, {self.region[1]}) (from last session)")
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
        # renamed from "auto_follow": the old default force-wrote true on
        # every close, so an existing true can't be told apart from a
        # choice. New key = everyone starts from the new default (off)
        # exactly once; re-enabling sticks from then on.
        self.follow_var.set(bool(cfg.get("follow_pauses", False)))
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
