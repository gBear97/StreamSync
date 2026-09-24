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

import os
import queue
import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import audio_capture
import capture
import controller
import windowctl
from controller import fmt_time
from players import EmbeddedPlayer, VLCError
from version import __version__


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.fullscreen = False
        self._preview_photo = None
        self.stream_hwnd = None      # pinned stream browser window
        self.stream_title = ""
        self._win_map = {}
        self._swapped = False        # stream window currently shown?
        self._was_fullscreen = False
        self._ext_hwnd = None

        root.title(f"StreamSync {__version__}")
        root.resizable(False, False)

        self._build_video_window()
        self._build_controls()

        try:
            embedded = EmbeddedPlayer(self.video_frame.winfo_id())
        except VLCError as e:
            if "--selftest" in sys.argv:
                # nobody can dismiss a modal in an unattended run
                print(f"SELFTEST: VLC unavailable - {e}", file=sys.stderr)
                raise SystemExit(2)
            messagebox.showerror("StreamSync - VLC problem", str(e))
            raise SystemExit(1)
        self.ctl = controller.SyncController(self.q, embedded)
        self.embedded = embedded

        self._load_config()
        self._apply_method_visibility()
        self._populate_audio_devices()
        self._refresh_windows()
        self._install_hotkeys()

        self.ctl.start()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(80, self._poll_queue)
        root.after(700, self._tick_time)

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
        if self.player_var.get() == "external":
            err = self.ctl.use_external(self.mute_var.get())
            if err:
                messagebox.showerror("StreamSync", err)
                self.player_var.set("embedded")
                return
            self.video_win.withdraw()
        else:
            self.ctl.use_embedded()
            if self.ctl.video_path:
                self.video_win.deiconify()
        self._on_mute_toggle()
        self._save_config()

    @property
    def player(self):
        return self.ctl.player

    @property
    def external(self):
        return self.ctl.external

    # ------------------------------------------------------------- actions

    def _choose_file(self):
        path = filedialog.askopenfilename(
            title="Choose the local copy of the film",
            filetypes=[("Video files", "*.mkv *.mp4 *.avi *.mov *.m4v *.ts *.webm *.wmv"),
                       ("All files", "*.*")])
        if not path:
            return
        self.file_lbl.config(text=Path(path).name)
        try:
            self.ctl.load_file(path)
        except VLCError as e:
            messagebox.showerror("StreamSync", str(e))
            return
        if self.player is self.embedded:
            self.video_win.deiconify()
            self._set_status(f"Loaded {Path(path).name}. Playback starts on first sync.")
        else:
            self._set_status("Opening in external VLC...")
        self._save_config()

    def _select_region(self):
        region = capture.RegionSelector(self.root).select()
        if region:
            self.ctl.region = region
            self.region_lbl.config(
                text=f"{region[2]}x{region[3]} at ({region[0]}, {region[1]})")
            self._set_status("Region set.")
            self._save_config()

    def _select_facecam_rect(self):
        if not self.ctl.region:
            messagebox.showinfo("StreamSync",
                                "Set the capture region first, then drag the "
                                "facecam zone inside it.")
            self.facecam_var.set("none")
            return
        rect = capture.RegionSelector(self.root).select()
        if not rect:
            self.facecam_var.set("none")
            return
        zone = controller.zone_in_region(self.ctl.region, rect)
        if zone is None:
            messagebox.showinfo("StreamSync", "That zone is outside the capture "
                                              "region - try again.")
            self.facecam_var.set("none")
            return
        self.ctl.facecam_rect = zone
        x0, y0, x1, y1 = zone
        self._set_status(f"Ignoring zone x {x0:.2f}-{x1:.2f}, y {y0:.2f}-{y1:.2f} "
                         "of the frame during video matching.")
        self._save_config()

    def _ready(self, need_region):
        if not self.ctl.video_path:
            messagebox.showinfo("StreamSync", "Choose a video file first.")
            return False
        if need_region and not self.ctl.region:
            messagebox.showinfo("StreamSync", "Select the stream's capture region first.")
            return False
        return True

    def _sync(self, resync=False):
        if self.ctl.busy:
            return
        method = self.method_var.get()
        if not self._ready(need_region=(method == "video")):
            return
        video = {"mute": self.mute_var.get()}
        if method == "video":
            self._set_status("Capturing stream frames...")
            video.update(mask=controller.build_mask(self.facecam_var.get(),
                                                    self.ctl.facecam_rect),
                         mirror=self.mirror_var.get(),
                         hidden=self._hide_overlapping_windows())
        run = self.ctl.resync if resync else self.ctl.sync
        try:
            started = run(self.hint_var.get(), self.window_var.get(), method,
                          **video)
        except ValueError as e:
            if video.get("hidden"):
                for win in video["hidden"]:
                    win.deiconify()
            messagebox.showerror("StreamSync", str(e))
            return
        if not started:
            for win in video.get("hidden") or ():
                win.deiconify()      # a sync was already running
        if started:
            self.sync_btn.state(["disabled"])
            self.resync_btn.state(["disabled"])

    def _resync(self):
        self._sync(resync=True)

    def _hide_overlapping_windows(self):
        left, top, w, h = self.ctl.region
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
        try:
            interval = self.interval_var.get()
        except (ValueError, tk.TclError):
            interval = 30
        self.ctl.set_auto(self.auto_var.get(), self.follow_var.get(), interval)
        self._save_config()

    # ------------------------------------------------------------- playback

    def _nudge(self, delta):
        offset = self.ctl.nudge(delta)
        self.offset_lbl.config(text=f"offset: {offset:+.2f}s")

    def _reset_offset(self):
        self.ctl.reset_offset()
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
        return self.ctl.session_running()

    def _host_dialog(self):
        if self._session_running():
            messagebox.showinfo("StreamSync", "Leave the current session first.")
            return
        if not self.ctl.video_path:
            messagebox.showinfo("StreamSync", "Choose the film file first.")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Host a session")
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=12)
        frm.grid()

        ttk.Label(frm, text="Relay server").grid(row=0, column=0, sticky="w")
        relay_var = tk.StringVar(value=self.ctl.relay_url)
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
            self.ctl.host(relay_var.get().strip(),
                          pw_var.get().strip() or None,
                          from_player=src_var.get() == "player",
                          mic_name=mic_var.get() or None,
                          delay_hint=float(delay_var.get()),
                          title=Path(self.ctl.video_path).stem)
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
        if not self.ctl.video_path:
            messagebox.showinfo("StreamSync", "Choose your copy of the film first.")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Join a session")
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=12)
        frm.grid()

        ttk.Label(frm, text="Relay server").grid(row=0, column=0, sticky="w")
        relay_var = tk.StringVar(value=self.ctl.relay_url)
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
            self.ctl.join(relay_var.get().strip(), code_var.get().strip().upper(),
                          pw_var.get().strip() or None, self.mute_var.get())
            self.leave_btn.state(["!disabled"])
            self._save_config()
            dlg.destroy()

        ttk.Button(frm, text="Join", command=start).grid(
            row=3, column=1, sticky="e", pady=(12, 0))
        dlg.grab_set()

    def _leave_session(self):
        self.ctl.leave()
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
        if not self.swap_var.get() or show == self._swapped or not self.ctl.video_path:
            return
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
                    if self.fullscreen:
                        self.video_win.attributes("-fullscreen", False)
                        self.fullscreen = False
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
        self.ctl.set_mute(self.mute_var.get())

    def _fullscreen_clicked(self):
        if self.player is self.embedded:
            self._set_fullscreen(not self.fullscreen)
        else:
            self.external.fullscreen_toggle()

    def _set_fullscreen(self, flag):
        self.fullscreen = flag
        self.video_win.deiconify()
        self.video_win.attributes("-fullscreen", flag)

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
                    for win in payload[0] or ():
                        win.deiconify()
                elif kind == "busy_off":
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
            if self.ctl.audio_device:
                for n in items[1:]:
                    if self.ctl.audio_device.lower() in n.lower():
                        self.device_var.set(n)
                        break
            if not self.device_var.get():
                self.device_var.set(items[0])

    def _on_device_pick(self, _event=None):
        val = self.device_var.get()
        self.ctl.audio_device = "" if val.startswith("(default)") else val
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
        self.ctl.save_config({
            "window": self.window_var.get(),
            "hint": self.hint_var.get(),
            "method": self.method_var.get(),
            "player": self.player_var.get(),
            "facecam": self.facecam_var.get(),
            "mirror": self.mirror_var.get(),
            "swap": self.swap_var.get(),
            "stream_title": self.stream_title,
        })

    def _load_config(self):
        cfg = self.ctl.load_config()
        path = self.ctl.film_to_restore(cfg)
        if path:
            self.ctl.load_file(path)
            self.file_lbl.config(text=Path(path).name)
        if self.ctl.region:
            r = self.ctl.region
            self.region_lbl.config(
                text=f"{r[2]}x{r[3]} at ({r[0]}, {r[1]}) (from last session)")
        if cfg.get("window"):
            self.window_var.set(cfg["window"])
        if cfg.get("hint"):
            self.hint_var.set(cfg["hint"])
        if cfg.get("method") in ("audio", "video"):
            self.method_var.set(cfg["method"])
        if cfg.get("facecam"):
            self.facecam_var.set(cfg["facecam"])
        self.mirror_var.set(bool(cfg.get("mirror", False)))
        self.interval_var.set(self.ctl.auto_interval)
        self.follow_var.set(self.ctl.auto_follow)
        self.swap_var.set(bool(cfg.get("swap", True)))
        self.stream_title = cfg.get("stream_title", "")
        if self.stream_title:
            self.streamwin_var.set(self.stream_title)
        # note: player choice is restored as "embedded"; external VLC is only
        # spawned when the user picks it, never on startup

    def _on_close(self):
        self._save_config()
        self.ctl.close()
        try:
            import keyboard
            keyboard.unhook_all()
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
        # libvlc leaves native threads running that a plain return would
        # wait on; an unattended check must never be able to hang.
        sys.stdout.flush()
        os._exit(0)


if __name__ == "__main__":
    main()
