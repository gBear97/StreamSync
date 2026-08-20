"""StreamSync for macOS.

Same engine as the Windows app (audio-first sync against a live stream),
with a Mac-shaped shell: a small, minimal main window - film, Sync,
Resync, nudges, status - and every less-used option living in the native
menu bar (Sync, Playback, Advanced menus).

Platform notes:
- Audio capture comes from the BlackHole virtual device (no loopback API
  on macOS); audio_capture handles the routing details.
- The "embedded" player draws into our own film window: libvlc opens no
  window of its own on macOS, so macvideo gives it an NSView to render
  into. Fullscreen belongs to that window, not to libvlc.
- The facecam swap activates/hides the browser app via AppleScript.
- The live tracker (_auto_loop and its helpers) is the same phase-locked
  tracker as app.py's, line for line; only the shell around it differs.
"""

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
import macvideo
import macwindowctl
import matcher
import players
import session
from players import EmbeddedPlayer, ExternalPlayer, VLCError

CONFIG_PATH = Path.home() / ".streamsync.json"
log = logging.getLogger("streamsync.mac")
BURST_FRAMES = 4
BURST_SPACING = 1 / 3
AUDIO_SYNC_SECONDS = 6.0
# Live tracker (_auto_loop): a sliding window of the film's own audio is
# the expectation; every captured second is correlated against it.
TRK_REF_BACK = 6.0         # expectation window: this far behind the spot...
TRK_REF_AHEAD = 34.0       # ...to this far ahead (one decode, slides along)
TRK_LOCK_SCORE = 0.22      # a block correlating this well = still locked
TRK_LOCK_SLACK = 3.0
TRK_ACT_SCORE = 0.55       # corrective ACTIONS (seek, resume) need real
                           # confidence: mismatched-but-loud audio scores
                           # up to ~0.45 on self-similar films
TRK_LOST_AFTER = 12        # degraded-with-energy blocks before a bounded
                           # wide search - a jump past the expectation
                           # window would otherwise strand the tracker
TRK_PAUSE_MISSES = 2       # MISSING blocks (in the recent window) = pause
TRK_MISS_WINDOW = 12       # ...counted over this many recent blocks, so a
                           # talked-over pause is caught at the breath gaps
TRK_MISSING_RATIO = 0.25   # a block this far under its EXPECTED energy is
                           # missing the film, whatever its score says
TRK_ABS_SILENCE = 1e-4     # capture this quiet is DEAD AIR (~-80 dBFS):
                           # the film is missing no matter what, so pause
                           # detection works before any calibration
TRK_SEED_N = 10            # the gain seeds from the MINIMUM of this many
                           # in-slack locks - commentary only ADDS energy,
                           # so the min converges on the clean ratio while
                           # single samples can be talk-inflated 6x
TRK_LO_HZ = 80.0           # sub-bass band: film scores/effects live down
                           # here, streamer mics are high-passed ~80-100Hz
                           # - so missing sub-bass under LOUD talk still
                           # means the film stopped (no breath gap needed)
TRK_LO_FLOOR = 0.003       # ...but only when the film SHOULD have it
TRK_LO_RATIO = 0.03        # a paused film leaves ~ZERO sub-bass (voices
                           # are high-passed), while a duck still leaves
                           # 10-20% - so the bar sits far below any duck
                           # a streamer would actually use (~-30dB)
TRK_EXP_FLOOR = 0.005      # expected audio quieter than this is silence -
                           # a faithful silent passage proves nothing
TRK_RESUME_ENERGY = 0.05   # a resume-counting block must carry SOME of
                           # the energy it claims to match - a bar low
                           # enough that an over-learned gain (talk can
                           # inflate it several-fold) can never hold a
                           # genuine resume hostage, yet dead air (the
                           # phantom-resume source) still fails it
TRK_MICRO_MIN = 0.15       # absorb drift above this smoothly...
TRK_MICRO_MAX = 3.0        # ...up to the lock slack: every in-slack
                           # error must have an owner (a ~2s rebuffer
                           # would otherwise be held forever as "locked"),
                           # and beyond the slack the seek paths take over
TRK_MICRO_COOLDOWN = 8.0
TRK_RESUME_WIN = 12.0      # resume watch reach around the pause point
TRK_SKIP_AFTER = 15.0      # nothing there yet -> one bounded wider search
TRK_SKIP_SPAN = 300.0
TRK_FAIL_GIVEUP = 3        # a check failing the SAME way this many times
                           # running is permanent - the film's drive gone,
                           # the file moved - not a blip: say so once,
                           # disarm, and stop the churn

# --- the reflex layer: sub-second pause/resume following. The slow layer
# above PROVES things on 2s of correlation; the reflex ACTS on 0.5s of
# band energy and lets the slow layer contradict it. Wrong guesses are
# undone inside the absorbable window, so acting early is safe - the one
# thing the reflex may never do is hold a freeze the correlator disputes.
TRK_R_CHUNK = 0.25         # the Listener's native chunk - judged one at
                           # a time, four to the slow layer's block
TRK_R_BANDS = ((1.0, 80.0), (4000.0, 7000.0), (1500.0, 4000.0))
                           # bands a voice cannot OWN outright: speech
                           # has no sub-bass at all, holds 4-7k only in
                           # sibilant bursts (7k cap = the reference is
                           # decoded at 16k), and - for films carrying
                           # nothing else, like an old mono master - the
                           # upper mids, which speech DOES reach: that
                           # band may only ever arm against the
                           # streamer's own measured talk ceiling
TRK_R_VOICE = (300.0, 1500.0)  # where speech lives - context, never a trigger
TRK_R_POOL = 1.0           # expectation min-pooled +/- this many seconds:
                           # the playhead estimate may sit that far off,
                           # and a scene cut must not read as a collapse
                           # (the slow layer's exact trick, same reason)
TRK_R_RATIO = 0.10         # heard under a tenth of the scaled expectation
                           # in EVERY armed band = the film left the mix;
                           # deep enough that a duck cannot reach it
TRK_R_NEED = 2             # consecutive collapsed chunks to freeze; 3 in
                           # wary mode (recent undos = stalls tonight)
TRK_R_FRESH = 3.0          # the reflex extrapolates the last LOCK's
                           # position, never the raw player clock - and
                           # only while the chunk STARTS within this long
                           # of the anchor stamp (in-cadence chunks start
                           # 2.00-2.75s after it: a full chunk of margin)
TRK_R_ARM_ERR = 0.4        # ...and only while measured lag is small:
                           # indexing quarter-second envelopes off a
                           # drifted clock reads transitions as pauses
TRK_R_STALE = 0.75         # never ACT on a chunk older than this (decode
                           # stalls backlog the queue; count as evidence,
                           # never as a trigger)
TRK_R_CONFIRM = 2.5        # probation: a reflex freeze has this long to
                           # be contradicted before it is announced; an
                           # undo inside it leaves under 3s of error -
                           # inside what the rate pulses can absorb
TRK_R_UNDO_N = 2           # coherent ADVANCING pairs at mere lock grade
                           # = the film never stopped. Certainty is not
                           # required to undo our own guess: the cheap
                           # failure is another reflex freeze.
TRK_R_REARM = 5            # after an undo, this many fresh small-lag
                           # locks before the reflex may fire again -
                           # never re-fire on the misalignment the undo
                           # itself created
TRK_R_SEED_N = 20          # per-band gains seed from the 20th percentile
                           # of this many locked blocks (a min ratchets
                           # low at band scale - outliers point DOWN here)
TRK_R_FLOOR = (0.003, 0.0015, 0.002)  # film-side arming floors per
                           # band: it must carry real film energy to
                           # testify - near-noise content never arms
TRK_R_TILT = 0.3           # speech rarely holds 4-7k above this fraction
                           # of its own voice-band energy for half a
                           # second: the high band arms only where the
                           # film should beat any plausible voice spill
TRK_R_CEIL_N = 30          # proven-pause blocks before the streamer's
                           # own measured talk ceilings gate arming
TRK_R_CEIL_X = 4.0         # ...requiring expectation this far above them
TRK_R_RES_RATIO = 0.5      # armed-band energy back at half expectation
                           # buys ONE optimistic resume...
TRK_R_VERIFY = 3.0         # ...which must earn a lock this fast or the
                           # film re-pauses where it stood
TRK_R_COOLDOWN = 8.0       # after a failed optimistic resume; a second
                           # failure disables optimism for the episode
TRK_R_WARY_S = 300.0       # two undos this recently = a stall-prone
                           # night: demand 3 chunks while both stand
LOW_CONFIDENCE = 0.55
BROWSERS = ("Safari", "Google Chrome", "Firefox", "Arc", "Brave Browser",
            "Microsoft Edge", "Opera", "Vivaldi")


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
        self.auto_follow = False     # experimental; Alex wants default off
        self.auto_interval = 30
        self.reflex_mode = "live"    # live | shadow (log, never act) | off
        self._closing = False
        self._swapped = False
        self._was_fullscreen = False
        self._swap_target = False    # state the last dispatched swap aims at
        self._swap_seq = 0
        self._swap_app = ""          # resolved browser, cached across swaps
        self._swap_q = queue.Queue()
        self._preview_photo = None
        self.external = None
        self.session = None          # active HostSession / ViewerSession
        self.relay_url = "ws://localhost:8765"

        root.title("StreamSync")
        root.resizable(False, False)

        # menu variables (created before menus reference them)
        self.method_var = tk.StringVar(value="audio")
        self.player_var = tk.StringVar(value="embedded")
        self.mute_var = tk.BooleanVar(value=True)
        self.auto_var = tk.BooleanVar(value=False)
        self.follow_var = tk.BooleanVar(value=False)
        self.interval_var = tk.IntVar(value=30)
        self.facecam_var = tk.StringVar(value="none")
        self.mirror_var = tk.BooleanVar(value=False)
        self.swap_var = tk.BooleanVar(value=True)
        self.device_var = tk.StringVar(value="")
        self.streamapp_var = tk.StringVar(value="")
        self.sub_var = tk.StringVar(value="")

        self._build_main()
        self._build_video_window()
        self._build_preview_window()

        try:
            self.embedded = EmbeddedPlayer(
                nsview=self.video_surface.view)
        except VLCError as e:
            messagebox.showerror("StreamSync - VLC problem", str(e))
            raise SystemExit(1)
        self.active_player = self.embedded

        self._load_config()
        self._build_menus()
        self._populate_audio_devices()

        threading.Thread(target=self._auto_loop, daemon=True).start()
        threading.Thread(target=self._swap_worker, daemon=True).start()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(80, self._poll_queue)
        root.after(700, self._tick_time)

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

        trow = ttk.Frame(frm)
        trow.grid(row=4, column=0, columnspan=6, sticky="w", pady=(12, 0))
        self.time_lbl = ttk.Label(trow, text="", foreground="#888",
                                  font=("SF Pro Text", 11))
        self.time_lbl.pack(side="left")
        # the tracker's glanceable truth, next to the clock (see _draw_lock)
        self.lock_canvas = tk.Canvas(trow, width=16, height=14,
                                     highlightthickness=0)
        self.lock_canvas.pack(side="left", padx=(10, 0))
        self.status_lbl = ttk.Label(frm, text="Open a film to begin.",
                                    foreground="#666", wraplength=380,
                                    font=("SF Pro Text", 11))
        self.status_lbl.grid(row=5, column=0, columnspan=6, sticky="w",
                             pady=(2, 0))

    def _build_video_window(self):
        """A Tk window whose content area libvlc renders into.

        libvlc has no window of its own on macOS, so we supply one. It
        stays hidden until the first sync - an empty black window before
        playback would just be confusing.
        """
        self.video_win = tk.Toplevel(self.root)
        self.video_win.title("StreamSync - Film")
        self.video_win.geometry("960x540")
        self.video_win.configure(bg="black")
        self.video_frame = tk.Frame(self.video_win, bg="black")
        self.video_frame.pack(fill="both", expand=True)
        self.video_win.protocol("WM_DELETE_WINDOW", self._hide_video_window)
        self.video_win.bind("<Escape>", lambda e: self._set_fullscreen(False))
        self.video_win.bind("<F11>", lambda e: self._toggle_fullscreen())
        self.video_win.bind("<space>", lambda e: self._toggle_pause())
        # the NSView has to exist before libvlc is told about it, and it
        # can only be created once Tk has actually mapped the window
        self.video_win.update_idletasks()
        self.video_surface = macvideo.VideoSurface(self.video_frame)
        self.video_win.withdraw()

    def _show_video_window(self):
        if not self.video_win.winfo_viewable():
            self.video_win.deiconify()
            self.video_win.update_idletasks()
            self.video_surface.sync()

    def _hide_video_window(self):
        self.video_win.withdraw()

    def _set_fullscreen(self, flag):
        self.fullscreen = bool(flag)
        if self.player is self.embedded:
            self._show_video_window()
            self.video_win.attributes("-fullscreen", self.fullscreen)
            self.video_win.update_idletasks()
            self.video_surface.sync()
        else:
            self.external.fullscreen_toggle()

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
        # follow does nothing without the tracker: show the dependency
        # (config was loaded before the menus exist, so apply it here)
        syncm.entryconfig("Follow Stream Pauses",
                          state="normal" if self.auto_enabled
                          else "disabled")
        self.sync_menu = syncm
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
        m.add_cascade(label="Advanced", menu=advm)

        self.root.config(menu=m)
        self.root.bind_all("<Command-o>", lambda e: self._choose_file())
        self.root.bind_all("<Command-s>", lambda e: self._sync())
        self.root.bind_all("<Command-r>", lambda e: self._resync())
        self.root.bind_all("<Command-p>", lambda e: self._toggle_pause())
        self.root.bind_all("<Shift-Command-f>",
                           lambda e: self._toggle_fullscreen())

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
            self.embedded.pause()
            self.active_player = self.external
            if self.video_path:
                t = self.embedded.time()

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
            self.active_player = self.embedded
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
        if self.player is self.embedded:
            try:
                self.embedded.load(path)
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
            if self.player is self.embedded:
                self._show_video_window()  # playback is about to start
            device = self.audio_device
            threading.Thread(
                target=self._audio_search_worker,
                args=(a, b, player, offset, mute, device), daemon=True).start()
        else:
            # The film window must NOT be revealed here: this method
            # screenshots the stream, and our own picture inside the
            # capture region would be matched instead of it. The worker
            # shows the window once the frames are grabbed.
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
            self.q.put(("showvideo", None))  # safe now: frames are captured
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
        try:
            self.auto_interval = max(10, int(self.interval_var.get()))
        except (ValueError, tk.TclError):
            self.auto_interval = 30
        # follow does nothing without the tracker: show the dependency
        self.sync_menu.entryconfig("Follow Stream Pauses",
                                   state="normal" if self.auto_enabled
                                   else "disabled")
        if self.auto_enabled:
            self._set_status("Auto re-sync on: tracking the stream live"
                             + (", following pauses."
                                if self.auto_follow else "."))
        self._save_config()

    def _clock_lag(self):
        """How far the active player's clock trails what it is emitting.

        Measured on this platform (see players.CLOCK_OUTPUT_LAG): libvlc's
        clock leads its CoreAudio output by ~0.17s here.
        """
        return (players.CLOCK_OUTPUT_LAG
                if self.active_player is self.embedded else 0.0)

    # --- live tracker: kept line-for-line identical to app.py's stack
    # (_tracker_ref through _auto_loop). Fix bugs there first, then copy;
    # test_tracker_reflex.py / test_tracker_giveup.py run against BOTH
    # shells and will catch a drifted copy.

    def _tracker_ref(self, film_start, center):
        """Features of what the stream SHOULD play around `center`.

        The tracker never searches the open film - it asks whether the
        incoming audio is where the film says it should be. No open
        search means a self-similar film has nowhere to teleport it, and
        one small decode slides along with the playhead.
        """
        lo = max(0.0, center - TRK_REF_BACK)
        x = audio_matcher.decode_audio(
            self.video_path, film_start + lo, TRK_REF_BACK + TRK_REF_AHEAD)
        sr = audio_matcher.SR
        n = max(1, len(x) // sr)
        # the film's own loudness, per second: "energy collapsed" is only
        # meaningful against what was SUPPOSED to be playing right now.
        # Sub-bass tracked separately - it is the band commentary can't
        # reach, so it stays readable under continuous talk.
        frames = x[:n * sr].reshape(n, sr)
        rms = np.sqrt(np.mean(frames ** 2, axis=1))
        X = np.fft.rfft(frames, axis=1)
        k = max(int(TRK_LO_HZ), 2)   # 1s frames at sr -> bin index == Hz
        rms_lo = np.sqrt(2.0 * np.sum(np.abs(X[:, 1:k]) ** 2, axis=1)) / sr
        # the reflex layer's expectation: per-chunk (0.25s) energies in
        # the voice-proof bands plus full-band, same normalization as the
        # per-second envelopes so one set of gains serves both
        cn = int(TRK_R_CHUNK * sr)
        m = max(1, len(x) // cn)
        cf = x[:m * cn].reshape(m, cn)
        CX = np.fft.rfft(cf, axis=1)

        def band(f0, f1):
            k0 = max(int(f0 * TRK_R_CHUNK), 1)
            k1 = max(int(f1 * TRK_R_CHUNK), k0 + 1)
            return (np.sqrt(2.0 * np.sum(np.abs(CX[:, k0:k1]) ** 2,
                                         axis=1)) / cn)

        renv = np.stack([band(*TRK_R_BANDS[0]), band(*TRK_R_BANDS[1]),
                         band(*TRK_R_BANDS[2])], axis=1)
        return lo, audio_matcher.features(x, sr), rms, rms_lo, renv

    @staticmethod
    def _band_rms(x, sr, hi_hz):
        """RMS of the sub-`hi_hz` band of a ~1s block (any sample rate)."""
        X = np.fft.rfft(x.astype(np.float64))
        k = max(int(hi_hz * len(x) / sr), 2)
        return float(np.sqrt(2.0 * np.sum(np.abs(X[1:k]) ** 2))
                     / max(len(x), 1))

    @staticmethod
    def _rband(x, sr, f0, f1):
        """RMS of the [f0, f1) Hz band of any block, normalized the same
        way as _band_rms and the reference envelopes."""
        X = np.fft.rfft(x.astype(np.float64))
        k0 = max(int(f0 * len(x) / sr), 1)
        k1 = max(int(f1 * len(x) / sr), k0 + 1)
        return float(np.sqrt(2.0 * np.sum(np.abs(X[k0:k1]) ** 2))
                     / max(len(x), 1))

    @staticmethod
    def _corr_block(ref_t0, W, C):
        """(position, score) of one captured block inside reference W."""
        scores = audio_matcher._corr_scores(W, C)
        i = int(np.argmax(scores))
        d = 0.0
        if 0 < i < len(scores) - 1:
            a, b, c2 = scores[i - 1], scores[i], scores[i + 1]
            denom = a - 2 * b + c2
            if abs(denom) > 1e-12:
                d = float(np.clip(0.5 * (a - c2) / denom, -1.0, 1.0))
        return ref_t0 + (i + d) * audio_matcher.HOP_S, float(scores[i])

    def _wide_relock(self, blocks, pause_point):
        """The streamer resumed somewhere else: a bounded search around
        the pause point, ambiguity refused - an unattended seek must be
        sure or stay put."""
        try:
            samples = np.concatenate([b for b, _ in blocks])
            feats = audio_matcher.prep_capture(samples,
                                               audio_capture.CAPTURE_SR)
            m = audio_matcher.find_match_audio_ex(
                self.video_path, feats,
                pause_point - TRK_SKIP_SPAN, pause_point + TRK_SKIP_SPAN,
                near=pause_point)
        except (RuntimeError, ValueError, matcher.MatchError):
            return None
        if (m.z >= audio_matcher.Z_OK and m.score >= audio_matcher.SCORE_OK
                and not m.ambiguous):
            return m.t, blocks[0][1]
        return None

    def _auto_loop(self):
        """The live follower: a phase-locked tracker, not a poller.

        We know exactly what the stream should sound like next - the
        film's own audio at the position we believe it holds - so a small
        window of expected features slides along with the playhead and
        every captured second is correlated against it. The lock score
        drives the meter; its lag is a continuous drift measurement,
        absorbed smoothly when small; and losing the lock while the
        energy signature collapses is a pause, caught within a couple of
        seconds. A streamer talking over the film degrades the lock but
        keeps the energy up - that never pauses anything.
        """
        listener = None
        listen_dev = None
        film_meta = None      # (path, container_start)
        ref = None            # (ref_t0, feats, per-second rms) expectation
        state = "off"         # off | locked | degraded | paused
        last_sent = None
        last_meter = 0.0
        lock_streak = 0
        gain = None           # EMA of capture-rms / film-rms while locked:
                              # the loopback and the decoded file sit on
                              # different gain chains, so "how loud should
                              # this second BE" needs a learned scale
        gain_seed = []        # commentary only ADDS energy, so the seed is
                              # the MINIMUM over the first TRK_SEED_N
                              # locks - one talk-polluted sample must not
                              # calibrate the pause detector
        gain_lo = None        # same idea for the sub-bass band
        gain_lo_seed = []
        trace = []            # per-block verdict chars, flushed to the log
        trace_scores = []     # every ~30s so a field night is diagnosable
        prev = None           # previous 1s block: evidence rolls in pairs
        classes = []          # recent block verdicts, for the pause rule
        deg_streak = 0        # consecutive not-locked blocks WITH energy
        resume_cand = None    # (t_found, t0) of the pending resume claim
        held = None           # (pause_point, path) of a tracker-held pause
                              # that survived a busy/disarm interruption -
                              # without it the guard wipes "paused" and the
                              # film would stay paused forever
        seen_lock = False     # the tracker has heard the film at least
                              # once this listener - until then NO energy
                              # arm may declare it missing (a wrong device
                              # or muted tab is silence too, and pausing
                              # on it would hold the film hostage)
        errs = []             # recent locked-block position errors
        big_err = None        # pending large-jump double-confirm
        fail_sig = None       # (type, message) of the failing check, and
        fail_n = 0            # its consecutive count: identical repeats
                              # mean permanent, and the loop gives up
        chunks = []           # the current second in 0.25s pieces: the
                              # reflex judges each one; every fourth
                              # assembles the block the slow layer knows
        r_anchor = None       # (position, t0) of the last in-slack lock -
                              # the reflex's ONLY notion of the playhead
        r_gain = [None] * 3   # per-band capture/film gains (LO, HI, MID)
        r_seed = [[], [], []]
        r_ceil = [[], [], []]  # the streamer's talk during proven pauses,
        r_ceilv = [None] * 3  # per band: live ceilings gating arming
        r_voice = None        # EMA of the voice band while tracking
        r_hits = 0            # consecutive collapsed chunks
        r_hit0 = None         # position where the current collapse began
        r_back = 0            # consecutive film-came-back chunks
        r_hold = None         # probation: (deadline, freeze wall-time) of
                              # a reflex pause not yet announced
        r_undo_cand = None    # advancing evidence contradicting it
        r_undo_n = 0
        r_undone = []         # wall times of recent undos (wary mode)
        r_lockout = 0         # fresh locks owed before the reflex re-arms
        r_try = None          # (deadline,) of an optimistic resume under
                              # verification by the slow layer
        r_tries = 0           # optimistic attempts this pause episode
        r_cool = 0.0
        r_arm_n = r_tot_n = 0  # reflex coverage, reported with the trace
        micro_at = 0.0
        pause_point = None
        wide_at = 0.0
        recent = []           # last few raw blocks, for the wide re-lock

        def close_listener():
            nonlocal listener, ref, gain, gain_seed, errs, recent, prev
            nonlocal classes, deg_streak, resume_cand, gain_lo
            nonlocal gain_lo_seed, trace, trace_scores, seen_lock
            nonlocal chunks, r_anchor, r_gain, r_seed, r_ceil, r_ceilv
            nonlocal r_voice, r_hits, r_back, r_hold, r_undo_cand
            nonlocal r_undo_n, r_try
            if listener is not None:
                listener.close()
            listener = None
            ref, gain, prev, resume_cand = None, None, None, None
            gain_lo = None
            errs, recent, classes, gain_seed = [], [], [], []
            gain_lo_seed, trace, trace_scores = [], [], []
            deg_streak = 0
            seen_lock = False
            # reflex EVIDENCE dies with the device chain it was learned
            # on; reflex OWNERSHIP does not - a probation that loses its
            # listener quietly becomes a proven pause (state and
            # pause_point survive, the watcher takes over)
            chunks = []
            r_anchor, r_voice = None, None
            r_gain, r_seed = [None] * 3, [[], [], []]
            r_ceil, r_ceilv = [[], [], []], [None] * 3
            r_hits = r_back = r_undo_n = 0
            r_undo_cand, r_try, r_hold = None, None, None

        def trace_add(code, score=None):
            nonlocal trace, trace_scores, r_arm_n, r_tot_n
            trace.append(code)
            if score is not None:
                trace_scores.append(score)
            if len(trace) >= 30:
                sc = trace_scores
                log.info(
                    "tracker trace: %s gain=%s lo=%s score p50=%s "
                    "reflex=%s",
                    "".join(trace),
                    f"{gain:.2f}" if gain is not None
                    else f"seed {len(gain_seed)}/{TRK_SEED_N}",
                    f"{gain_lo:.2f}" if gain_lo is not None else "-",
                    f"{float(np.median(sc)):.2f}" if sc else "-",
                    # armed coverage: a reflex that cannot arm is a fact
                    # a field night must be able to see
                    f"{100 * r_arm_n // r_tot_n}%" if r_tot_n else "-")
                trace, trace_scores = [], []
                r_arm_n = r_tot_n = 0

        def meter(new_state, strength=0.0):
            nonlocal state, last_sent, last_meter
            state = new_state
            key = (new_state, round(strength, 1))
            now = time.monotonic()
            if key != last_sent or now - last_meter >= 2.0:
                self.q.put(("lock", new_state, strength))
                last_sent, last_meter = key, now

        def r_ceil_val(b):
            if len(r_ceil[b]) < TRK_R_CEIL_N:
                return None
            if r_ceilv[b] is None:
                r_ceilv[b] = float(np.percentile(r_ceil[b], 90))
            return r_ceilv[b]

        def r_try_fallback():
            """An unverified optimistic resume is OWNERSHIP, not
            evidence: the player is PLAYING on our guess, and whatever
            interrupted us (a dead listener, a device change, a manual
            sync) killed the verification. Park the film back on its
            proven pause - exactly what the in-loop deadline does - so
            the watcher, or held, owns it again instead of the film
            free-running over a paused stream."""
            nonlocal r_try, r_cool, wide_at
            if r_try is None or pause_point is None:
                return
            r_try = None
            r_cool = time.monotonic() + TRK_R_COOLDOWN
            wide_at = time.monotonic()
            try:
                self.embedded.pause()   # r_try is embedded-only
            except Exception:
                pass
            meter("paused")
            log.info("reflex: verification interrupted - back to the "
                     "proven pause at %s", fmt_time(pause_point))

        def _reflex_watch(player, chunk, t0c, now, act):
            """Pause side of the reflex: fire when every armed band says
            the film's contribution left the mix for half a second."""
            nonlocal r_hits, r_hit0, r_voice, r_arm_n, r_tot_n, r_hold
            nonlocal r_tries, r_undo_cand, r_undo_n, pause_point, wide_at
            nonlocal lock_streak, deg_streak, big_err, resume_cand, classes
            r_tot_n += 1
            if (not self.auto_follow or self.busy
                    or self._session_running() or not seen_lock
                    or gain is None or r_anchor is None or r_lockout
                    or r_try is not None):
                r_hits = 0
                return
            age = t0c + TRK_R_CHUNK - r_anchor[1]
            # gate the chunk's START age: in-cadence chunks start 2.00 to
            # 2.75s after the anchor stamp (the 2s pair's start, refreshed
            # once a second), so a full chunk of margin separates them
            # from the boundary - the pump's clock easing must never
            # decide it. The first chunk after a missed lock starts at
            # 3.00 exactly and stays refused.
            if (not 0.0 <= age - TRK_R_CHUNK < TRK_R_FRESH
                    or len(errs) < 3
                    or abs(float(np.median(errs[-3:]))) > TRK_R_ARM_ERR):
                r_hits = 0
                return
            # stream-truth playhead: the last lock's position plus real
            # time since - the player clock never touches the reflex
            pos = r_anchor[0] + age
            renv = ref[4]
            i = int((pos - ref[0]) / TRK_R_CHUNK)
            w = int(TRK_R_POOL / TRK_R_CHUNK)
            if i - w < 0 or i + w >= len(renv):
                return   # window about to slide; hold the evidence
            exp_b = renv[i - w:i + w + 1].min(axis=0)
            csr = audio_capture.CAPTURE_SR
            h = (self._rband(chunk, csr, *TRK_R_BANDS[0]),
                 self._rband(chunk, csr, *TRK_R_BANDS[1]),
                 self._rband(chunk, csr, *TRK_R_BANDS[2]))
            h_full = float(np.sqrt(np.mean(chunk * chunk)))
            h_vo = self._rband(chunk, csr, *TRK_R_VOICE)
            armed = []
            for b in (0, 1, 2):
                if r_gain[b] is None or exp_b[b] < TRK_R_FLOOR[b]:
                    continue
                scaled = r_gain[b] * exp_b[b]
                if b == 1 and scaled < 3.0 * TRK_R_TILT * h_vo:
                    continue   # a loud voice could fake this much 4-7k
                ceil = r_ceil_val(b)
                if b == 2 and ceil is None:
                    continue   # the upper mids NEVER arm on a guess -
                    #             only over this streamer's measured talk
                if ceil is not None and scaled < TRK_R_CEIL_X * ceil:
                    continue   # this streamer's talk reaches too close
                armed.append(b)
            if not armed:
                r_hits = 0
                r_voice = (h_vo if r_voice is None
                           else 0.9 * r_voice + 0.1 * h_vo)
                return
            r_arm_n += 1
            if not all(h[b] < TRK_R_RATIO * r_gain[b] * exp_b[b]
                       for b in armed):
                r_hits = 0
                r_voice = (h_vo if r_voice is None
                           else 0.9 * r_voice + 0.1 * h_vo)
                return
            need = TRK_R_NEED
            if (len(r_undone) >= 2
                    and now - r_undone[-2] <= TRK_R_WARY_S):
                need += 1   # wary: tonight stalls or drops out a lot
            if (h_full < TRK_ABS_SILENCE and r_voice is not None
                    and r_voice >= 10 * TRK_ABS_SILENCE):
                # digital zero right after healthy capture smells like a
                # device dropout, not a pause - demand one more chunk
                need = max(need, TRK_R_NEED + 1)
            r_hits += 1
            if r_hits == 1:
                # remember where the collapse STARTED: if stale backlog
                # interleaves, the acting chunk is not the start
                r_hit0 = pos - TRK_R_CHUNK
            if r_hits < need:
                return
            if not act:
                return   # a backlogged chunk may complete the evidence;
                         # only a live one may act on it
            r_hits = 0
            if self.reflex_mode == "shadow":
                log.info("reflex[shadow]: would freeze at %s (bands %s)",
                         fmt_time(pos), armed)
                return
            player.pause()
            pause_point = (r_hit0 if r_hit0 is not None
                           else pos - need * TRK_R_CHUNK)
            wide_at = now
            r_hold = (now + TRK_R_CONFIRM, time.perf_counter())
            r_undo_cand, r_undo_n, r_tries = None, 0, 0
            lock_streak = deg_streak = 0
            big_err, resume_cand = None, None
            classes = []
            meter("paused")
            log.info("reflex: film left the mix at %s (bands %s) - "
                     "froze, awaiting confirmation", fmt_time(pause_point),
                     armed)

        def _reflex_resume(player, chunk, t0c, now, act):
            """Resume side: film-shaped energy back at the pause point
            buys ONE optimistic resume the slow layer must then confirm."""
            nonlocal r_back, r_try, r_tries, r_cool
            if (self.busy or self._session_running() or pause_point is None
                    or now < r_cool or r_tries >= 2 or gain is None):
                r_back = 0
                return
            renv = ref[4]
            i = int((pause_point - ref[0]) / TRK_R_CHUNK)
            w = int(TRK_R_POOL / TRK_R_CHUNK)
            lo_i, hi_i = max(0, i - w), min(len(renv), i + w + 1)
            if lo_i >= hi_i:
                return
            # a resume announces itself LOUD: judge against the window's
            # peak - resumes into quiet film wait for the proven watcher
            exp_b = renv[lo_i:hi_i].max(axis=0)
            csr = audio_capture.CAPTURE_SR
            h_lo = self._rband(chunk, csr, *TRK_R_BANDS[0])
            h_hi = self._rband(chunk, csr, *TRK_R_BANDS[1])
            h_vo = self._rband(chunk, csr, *TRK_R_VOICE)
            h_full = float(np.sqrt(np.mean(chunk * chunk)))
            j = min(max(int(pause_point - ref[0]), 0), len(ref[2]) - 1)
            exp_full = float(np.max(ref[2][max(0, j - 1):j + 2]))
            back = False
            if (r_gain[0] is not None and exp_b[0] >= TRK_R_FLOOR[0]
                    and h_lo >= TRK_R_RES_RATIO * r_gain[0] * exp_b[0]):
                back = True   # sub-bass came back: no voice can do that
            elif (r_gain[1] is not None and exp_b[1] >= TRK_R_FLOOR[1]
                    and h_hi >= TRK_R_RES_RATIO * r_gain[1] * exp_b[1]
                    and h_hi >= 2.0 * TRK_R_TILT * h_vo
                    and h_full >= TRK_R_RES_RATIO * gain * exp_full):
                back = True   # high band AND the whole mix filled back in
            elif (r_gain[2] is not None and exp_b[2] >= TRK_R_FLOOR[2]
                    and r_ceil_val(2) is not None
                    and self._rband(chunk, csr, *TRK_R_BANDS[2])
                    >= max(TRK_R_RES_RATIO * r_gain[2] * exp_b[2],
                           TRK_R_CEIL_X * r_ceil_val(2))
                    and h_full >= TRK_R_RES_RATIO * gain * exp_full):
                back = True   # upper mids well past this streamer's talk
            if not back:
                r_back = 0
                return
            r_back += 1
            if r_back < TRK_R_NEED:
                return
            if not act:
                return   # backlogged evidence completes streaks, never acts
            r_back = 0
            if self.reflex_mode == "shadow":
                log.info("reflex[shadow]: would resume at %s",
                         fmt_time(pause_point))
                return
            r_tries += 1
            # the film restarted no later than the FIRST back-chunk: the
            # seek anchors there, not at the chunk that merely confirmed
            player.sync_seek(pause_point,
                             t0c - (TRK_R_NEED - 1) * TRK_R_CHUNK,
                             self.offset)
            r_try = (time.monotonic() + TRK_R_VERIFY,)
            meter("degraded", 0.0)
            log.info("reflex: optimistic resume #%d at %s", r_tries,
                     fmt_time(pause_point))

        def reflex_tick(player, chunk, t0c):
            """The fast half of the tracker: judge one 0.25s chunk against
            what the film should be emitting right now, and act - the
            slow layer keeps the right to contradict every action."""
            nonlocal r_hold, ref
            now = time.monotonic()
            # the deadline outranks every gate below: a probation must
            # expire even if the player was switched out from under it -
            # the freeze belongs to the EMBEDDED player either way
            if (r_hold is not None and now >= r_hold[0]
                    and not self.busy and not self._session_running()):
                r_hold = None
                if not self.auto_follow:
                    try:   # follow was unticked mid-probation: give the
                        self.embedded.resume()   # film back
                    except Exception:
                        pass
                    meter("degraded", 0.0)
                    log.info("reflex: probation lapsed with follow off "
                             "- resumed")
                else:
                    ref = None   # the watcher wants its own window: more
                    #              behind the point, for rewind-resumes
                    self.q.put(("swap", True))
                    self.q.put((
                        "status",
                        "Auto: the stream stopped playing the film "
                        "- pausing to match. Watching for the resume..."))
                    log.info("reflex: pause confirmed at %s",
                             fmt_time(pause_point))
                return
            if (self.reflex_mode == "off" or player is not self.embedded
                    or ref is None or len(ref) < 5 or film_meta is None):
                return
            # a backlogged chunk (a decode ran) still carries evidence -
            # it just may not ACT: the act flag gates the triggers only
            act = time.perf_counter() - t0c <= TRK_R_STALE
            if state == "paused":
                if r_hold is None:
                    _reflex_resume(player, chunk, t0c, now, act)
            else:
                _reflex_watch(player, chunk, t0c, now, act)

        while not self._closing:
            if (not self.auto_enabled or self.busy or not self.video_path
                    or self._session_running()):  # sessions own the playhead
                r_try_fallback()   # a playing, unverified resume must be
                #                    parked BEFORE held looks at state
                if state == "paused":
                    # remember the pause WE hold: the interruption (a
                    # manual sync, a toggle, a session) must not orphan
                    # a film the tracker paused and still owes a resume
                    held = (pause_point, self.video_path)
                if state != "off":
                    meter("off")
                lock_streak = 0
                big_err = None
                # an idle spell (busy, disarm, re-arm) starts the failure
                # count fresh - a user retrying deserves the full three
                fail_sig, fail_n = None, 0
                r_hits = r_back = r_undo_n = 0
                r_undo_cand = None
                close_listener()   # also clears streaks and candidates
                time.sleep(1.0)
                continue
            if held is not None:
                pp_h, path_h = held
                held = None
                if (path_h == self.video_path and self.active_player
                        and not self.active_player.is_playing()):
                    # still our pause: go back to watching for the resume
                    # (if the interruption resumed playback - a successful
                    # manual sync, the user pressing play - just track)
                    pause_point = pp_h
                    wide_at = time.monotonic()
                    meter("paused")
            if listener is None or listen_dev != self.audio_device:
                try:
                    r_try_fallback()   # a device change kills the verify
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
                chunk, t0_chk = listener.read(TRK_R_CHUNK)
            except Exception as e:
                self.q.put(("status", f"Auto listener failed: {e}"))
                r_try_fallback()   # a dead listener kills the verify
                close_listener()
                time.sleep(3.0)
                continue
            player = self.active_player
            check_failed = False
            check_ran = False
            try:
                # capture is film-agnostic (it is the stream's audio):
                # buffer the chunk before any film bookkeeping, so a
                # probe iteration never eats a quarter of a block
                chunks.append((chunk, t0_chk))
                del chunks[:-4]
                if film_meta is None or film_meta[0] != self.video_path:
                    film_meta = (self.video_path,
                                 matcher.probe(self.video_path)[1])
                    # a NEW film: nothing from the old timeline survives -
                    # not the pause point (it would be watched inside the
                    # wrong file), not the gain (different master levels),
                    # not the verdict history
                    ref, prev, gain, big_err = None, None, None, None
                    gain_lo = None
                    errs, classes, gain_seed, gain_lo_seed = [], [], [], []
                    trace, trace_scores = [], []
                    lock_streak = deg_streak = 0
                    seen_lock = False
                    resume_cand, pause_point, held = None, None, None
                    r_anchor, r_voice = None, None
                    r_gain, r_seed = [None] * 3, [[], [], []]
                    r_ceil, r_ceilv = [[], [], []], [None] * 3
                    r_hits = r_back = r_undo_n = r_lockout = r_tries = 0
                    r_undo_cand, r_hold, r_try = None, None, None
                    r_undone, r_cool = [], 0.0
                    if state == "paused":
                        meter("off")
                    continue
                reflex_tick(player, chunk, t0_chk)
                if len(chunks) < 4:
                    continue
                block = np.concatenate([c for c, _ in chunks])
                t0_blk = chunks[0][1]
                chunks = []
                check_ran = True   # a full slow-layer check begins here
                rms = float(np.sqrt(np.mean(block * block)))
                lo_rms = self._band_rms(block, audio_capture.CAPTURE_SR,
                                        TRK_LO_HZ)
                recent.append((block, t0_blk))
                del recent[:-3]
                # evidence rolls in overlapping 2s pairs: one second is
                # too little audio to correlate reliably, and near-silence
                # correlates as garbage-high (its energy divides to noise)
                pair, prev = prev, (block, t0_blk)
                if pair is None:
                    continue
                t0_cap = pair[1]
                try:
                    C = audio_matcher.prep_capture(
                        np.concatenate([pair[0], block]),
                        audio_capture.CAPTURE_SR)
                except matcher.MatchError:
                    C = None   # too quiet to featurize - judge by energy

                if state != "paused":
                    t_ref = player.time() if player.is_playing() else None
                    if t_ref is None:
                        meter("off")
                        continue
                    # where the film was when this pair STARTED playing,
                    # then where the STREAM should be for it: the film
                    # plus the player's clock lag, minus the user's offset
                    film_at = t_ref - (time.perf_counter() - t0_cap)
                    expect = film_at + self._clock_lag() - self.offset
                    if (ref is None or expect < ref[0] + 2.0
                            or expect + 2.0 > ref[0] + TRK_REF_BACK
                            + TRK_REF_AHEAD - 4.0):
                        ref = self._tracker_ref(film_meta[1], expect)
                        errs = []
                    if C is not None:
                        t_found, score = self._corr_block(ref[0], ref[1], C)
                        err = t_found - expect
                    else:
                        t_found, score, err = None, 0.0, None
                    # how loud the film ITSELF is in the second just heard
                    # - judged against the QUIETEST nearby second, because
                    # the playhead estimate can sit a second off and a
                    # loud/quiet boundary must not read as missing energy
                    i_exp = min(max(int(expect + 1.0 - ref[0]), 0),
                                len(ref[2]) - 1)
                    exp_ctr = float(ref[2][i_exp])
                    exp_rms = float(np.min(
                        ref[2][max(0, i_exp - 1):i_exp + 2]))
                    exp_lo = float(np.min(
                        ref[3][max(0, i_exp - 1):i_exp + 2]))
                    # energy is judged BEFORE the correlator gets a vote:
                    # a block missing the film's energy proves the film is
                    # not playing, whatever its (garbage) score says
                    if exp_rms < TRK_EXP_FLOOR:
                        cls = "quiet"    # film is silent here: no evidence
                    elif (C is not None and score >= TRK_ACT_SCORE
                          and abs(err) <= TRK_LOCK_SLACK):
                        # features are z-scored per band, so a confident
                        # in-place lock is VOLUME-INVARIANT proof the film
                        # is playing - it outranks a missing-energy read
                        # (a streamer ducking the film must not pause it;
                        # the gain EMA re-learns the new level instead)
                        cls = "lock"
                    elif C is not None and score >= TRK_ACT_SCORE:
                        # confident correlation at the WRONG lag still
                        # proves the film is playing (a jump, not a
                        # pause) - it must outrank every energy read or
                        # a stream seek gets misread as a pause
                        cls = "big"
                    elif seen_lock and (
                            rms < TRK_ABS_SILENCE
                            or (gain is not None
                                and rms < TRK_MISSING_RATIO * gain
                                * exp_rms)
                            or (gain_lo is not None
                                and exp_lo >= TRK_LO_FLOOR
                                and lo_rms < TRK_LO_RATIO * gain_lo
                                * exp_lo)):
                        # three ways to be missing: dead air (needs no
                        # calibration), total energy far under expectation,
                        # or the film's sub-bass gone while a mic-high-
                        # passed voice talks over the pause. None may
                        # fire before the film has been heard once.
                        cls = "miss"
                        log.debug(
                            "tracker miss: rms=%.5f lo=%.5f exp=%.4f "
                            "exp_lo=%.4f gain=%s gain_lo=%s score=%.2f",
                            rms, lo_rms, exp_rms, exp_lo,
                            f"{gain:.2f}" if gain is not None else "-",
                            f"{gain_lo:.2f}" if gain_lo is not None
                            else "-", score)
                    elif (C is not None and score >= TRK_LOCK_SCORE
                          and abs(err) <= TRK_LOCK_SLACK):
                        cls = "lock"
                    else:
                        cls = "deg"      # voice over the film, most likely
                    classes.append(cls)
                    del classes[:-TRK_MISS_WINDOW]
                    trace_add({"lock": "L", "miss": "M", "big": "B",
                               "deg": "D", "quiet": "q"}[cls],
                              score if C is not None else None)

                    if r_try is not None:
                        if cls == "lock":
                            # the optimistic resume found the film again:
                            # now it is a real resume - say so
                            r_try = None
                            r_tries = 0
                            self.q.put(("swap", False))
                            self.q.put(("status",
                                        "Auto: stream resumed - "
                                        "following."))
                            log.info("reflex: resume confirmed")
                        elif (time.monotonic() >= r_try[0]
                                and not self.busy
                                and not self._session_running()):
                            # nothing locked: that energy was not the
                            # film. Back to the pause, where it stood -
                            # on the EMBEDDED player, whatever is active
                            self.embedded.pause()
                            r_try = None
                            r_cool = time.monotonic() + TRK_R_COOLDOWN
                            wide_at = time.monotonic()
                            meter("paused")
                            log.info("reflex: resume not confirmed - "
                                     "re-pausing (attempt %d)", r_tries)
                            continue

                    if cls == "lock":
                        lock_streak += 1
                        deg_streak = 0
                        big_err = None
                        seen_lock = True
                        if exp_ctr >= TRK_EXP_FLOOR:
                            # any in-slack lock calibrates - real streams
                            # rarely reach confident scores, and waiting
                            # for them left the pause detector disabled
                            # all night in the field. The talk-pollution
                            # protection is the MIN over TRK_SEED_N
                            # samples (commentary only ever ADDS energy)
                            # plus the lower-envelope EMA afterwards:
                            # sink fast, rise reluctantly.
                            g = rms / exp_ctr
                            if gain is None:
                                gain_seed.append(g)
                                if len(gain_seed) >= TRK_SEED_N:
                                    gain = min(gain_seed)
                                    log.info(
                                        "tracker: gain calibrated %.3f "
                                        "(min of %d locks)", gain,
                                        TRK_SEED_N)
                            elif g < gain:
                                gain = 0.7 * gain + 0.3 * g
                            else:
                                gain = 0.98 * gain + 0.02 * g
                        exp_lo_ctr = float(ref[3][i_exp])
                        if exp_lo_ctr >= TRK_LO_FLOOR:
                            g2 = lo_rms / exp_lo_ctr
                            if gain_lo is None:
                                gain_lo_seed.append(g2)
                                if len(gain_lo_seed) >= TRK_SEED_N:
                                    gain_lo = min(gain_lo_seed)
                                    log.info(
                                        "tracker: sub-bass gain "
                                        "calibrated %.3f", gain_lo)
                            elif g2 < gain_lo:
                                gain_lo = 0.7 * gain_lo + 0.3 * g2
                            else:
                                gain_lo = 0.98 * gain_lo + 0.02 * g2
                        errs.append(err)
                        del errs[:-5]
                        # the reflex extrapolates from here, and earns its
                        # way back after an undo only through steady locks
                        r_anchor = (t_found, t0_cap)
                        if (r_lockout and len(errs) >= 3
                                and abs(float(np.median(errs[-3:])))
                                <= TRK_R_ARM_ERR):
                            r_lockout -= 1
                        # per-band reflex gains: center-learned like the
                        # full-band gain, only where the film actually
                        # carries the band; percentile seed - a min
                        # ratchets low at band scale
                        j0 = int((t_found + 1.0 - ref[0]) / TRK_R_CHUNK)
                        if 0 <= j0 and j0 + 4 <= len(ref[4]):
                            # quadratic mean: the block's band RMS is the
                            # QM of its chunks' - an arithmetic mean would
                            # inflate the gain up to 2x on bursty content
                            rexp = np.sqrt(
                                (ref[4][j0:j0 + 4] ** 2).mean(axis=0))
                            for b in (0, 1, 2):
                                if rexp[b] < TRK_R_FLOOR[b]:
                                    continue
                                gb = (self._rband(
                                    block, audio_capture.CAPTURE_SR,
                                    *TRK_R_BANDS[b]) / float(rexp[b]))
                                if r_gain[b] is None:
                                    r_seed[b].append(gb)
                                    if len(r_seed[b]) >= TRK_R_SEED_N:
                                        r_gain[b] = float(np.percentile(
                                            r_seed[b], 20))
                                        log.info(
                                            "reflex: band %d gain "
                                            "calibrated %.4f", b,
                                            r_gain[b])
                                    continue
                                if gb < r_gain[b]:
                                    # sink fast but never off a cliff -
                                    # one codec-crushed block must not
                                    # ratchet the band dead
                                    r_gain[b] = max(
                                        0.7 * r_gain[b] + 0.3 * gb,
                                        0.25 * r_gain[b])
                                else:
                                    r_gain[b] = (0.98 * r_gain[b]
                                                 + 0.02 * gb)
                                if gain is not None:
                                    r_gain[b] = min(max(r_gain[b],
                                                        0.05 * gain),
                                                    20.0 * gain)
                        meter("locked", score)
                        now = time.monotonic()
                        if (len(errs) == 5
                                and now - micro_at >= TRK_MICRO_COOLDOWN):
                            med = float(np.median(errs))
                            if (TRK_MICRO_MIN < abs(med) < TRK_MICRO_MAX
                                    and not self.busy
                                    and not self._session_running()):
                                # the continuous lag IS the drift: absorb
                                # it with a gentle rate pulse, no seek
                                player.absorb_drift(med)
                                log.info("tracker: absorbing %+.2fs drift",
                                         med)
                                micro_at = now
                                errs = []
                    elif cls == "big":
                        # a strong match at the wrong lag is a real jump -
                        # if a second consecutive block agrees on it
                        lock_streak = 0
                        deg_streak += 1
                        if (big_err is not None
                                and abs(err - big_err) < 0.5
                                and not self.busy
                                and not self._session_running()):
                            player.sync_seek(t_found, t0_cap, self.offset)
                            self.q.put((
                                "status",
                                f"Auto: corrected {err:+.2f}s "
                                f"(score {score:.2f}, confirmed twice)."))
                            if r_try is not None:
                                # a confident corrective seek IS the
                                # verification: the film is playing
                                r_try, r_tries = None, 0
                                self.q.put(("swap", False))
                            big_err = None
                            deg_streak = 0
                            errs, classes, ref = [], [], None
                        else:
                            big_err = err
                        meter("degraded", score)
                    elif cls == "miss":
                        lock_streak = 0
                        deg_streak = 0
                        big_err = None
                        if (self.auto_follow
                                and classes.count("miss")
                                >= TRK_PAUSE_MISSES
                                and not self.busy
                                and not self._session_running()):
                            # the film's energy signature is gone - twice
                            # in the recent window, so even a pause masked
                            # by talk is caught at the breath gaps
                            player.pause()
                            wide_at = time.monotonic()
                            deg_streak, resume_cand = 0, None
                            ref, classes = None, []
                            if r_try is not None:
                                # the slow layer is overruling our
                                # optimistic resume: count it as the
                                # failed attempt it is, keep the already-
                                # announced pause point - the stream
                                # never moved - and say nothing twice
                                if player is not self.embedded:
                                    self.embedded.pause()
                                r_try = None
                                r_cool = (time.monotonic()
                                          + TRK_R_COOLDOWN)
                                meter("paused")
                                log.info("reflex: optimistic resume "
                                         "overruled by the energy check "
                                         "- back to the pause")
                            else:
                                pause_point = expect
                                self.q.put(("swap", True))
                                self.q.put((
                                    "status",
                                    "Auto: the stream stopped playing "
                                    "the film - pausing to match. "
                                    "Watching for the resume..."))
                                meter("paused")
                        else:
                            meter("degraded", 0.0)
                    elif cls == "deg":
                        lock_streak = 0
                        deg_streak += 1
                        if (deg_streak >= TRK_LOST_AFTER
                                and len(recent) >= 3 and not self.busy
                                and not self._session_running()):
                            # energy present but no lock for a while: the
                            # stream may have jumped past the expectation
                            # window. One bounded, sure search - and only
                            # seek if it lands somewhere genuinely new.
                            deg_streak = 0
                            hit = self._wide_relock(recent, expect)
                            # the hit is anchored at recent[0], one block
                            # BEFORE the pair - shift the prediction to
                            # the same instant or the acceptance band is
                            # off by that second
                            if (hit is not None
                                    and abs(hit[0]
                                            - (expect
                                               - (t0_cap - hit[1])))
                                    > TRK_LOCK_SLACK):
                                t_w, t0_w = hit
                                player.sync_seek(t_w, t0_w, self.offset)
                                self.q.put((
                                    "status",
                                    "Auto: the stream moved - re-locked "
                                    f"at {fmt_time(t_w)}."))
                                if r_try is not None:
                                    # the wide search found the film
                                    # playing: verification satisfied
                                    r_try, r_tries = None, 0
                                    self.q.put(("swap", False))
                                errs, classes, ref = [], [], None
                                meter("locked", 0.5)
                                continue
                        meter("degraded", score)
                    else:  # "quiet": hold state, just heartbeat the meter
                        meter(state if state in ("locked", "degraded")
                              else "degraded", score)
                else:  # paused: watch the pause point itself, live
                    if ref is None:
                        ref = self._tracker_ref(
                            film_meta[1],
                            max(0.0, pause_point - TRK_RESUME_WIN
                                + TRK_REF_BACK))
                    if r_hold is not None:
                        # probation: our own freeze, not yet announced.
                        # Coherent ADVANCING evidence at mere lock grade
                        # proves the film never stopped - certainty is
                        # not required to undo our own guess; the cheap
                        # failure is another reflex freeze. (The tracking
                        # ref still covers the region ahead: it was NOT
                        # cleared at the freeze, so no decode stalls the
                        # 2.5s probation clock.)
                        sc = None
                        if C is not None:
                            t_f, sc = self._corr_block(ref[0], ref[1], C)
                            if sc >= TRK_LOCK_SCORE:
                                coh = (r_undo_cand is not None
                                       and abs((t_f - r_undo_cand[0])
                                               - (t0_cap
                                                  - r_undo_cand[1]))
                                       <= 1.0)
                                r_undo_n = r_undo_n + 1 if coh else 1
                                r_undo_cand = (t_f, t0_cap)
                            else:
                                r_undo_n, r_undo_cand = 0, None
                            if (r_undo_n >= TRK_R_UNDO_N
                                    and not self.busy
                                    and not self._session_running()):
                                # the freeze was applied to the EMBEDDED
                                # player; undo it there, whatever is
                                # active by now
                                self.embedded.resume()
                                behind = (time.perf_counter()
                                          - r_hold[1])
                                r_hold = None
                                r_undone.append(time.monotonic())
                                del r_undone[:-4]
                                r_lockout = TRK_R_REARM
                                r_undo_n, r_undo_cand = 0, None
                                lock_streak = 0
                                # errs stays: the median absorber must
                                # finish what one pulse cannot (1.5s cap,
                                # and the pulse may be busy - it reports,
                                # we do not insist)
                                self.embedded.absorb_drift(
                                    min(behind, 1.5))
                                meter("degraded", sc)
                                log.info("reflex: false alarm - resumed "
                                         "after %.1fs, absorbing", behind)
                                continue
                        trace_add("f", sc)
                        meter("paused")
                        continue
                    if C is None:
                        lock_streak = 0
                        resume_cand = None
                    else:
                        t_found, score = self._corr_block(ref[0], ref[1], C)
                        i_exp = min(max(int(t_found - ref[0]), 0),
                                    len(ref[2]) - 1)
                        exp_rms = float(np.min(
                            ref[2][max(0, i_exp - 1):i_exp + 2]))
                        # a resume claim must CARRY the energy it claims
                        # to match - silence scores are garbage, and they
                        # were resuming the film at phantom moments. It
                        # must also ADVANCE coherently: a real resume
                        # moves a second per second, spurious peaks
                        # scatter across the window.
                        heard = (exp_rms >= TRK_EXP_FLOOR
                                 and (gain is None
                                      or rms >= TRK_RESUME_ENERGY
                                      * gain * exp_rms))
                        if (score >= TRK_ACT_SCORE and heard
                                and abs(t_found - pause_point)
                                <= TRK_RESUME_WIN):
                            coherent = (
                                resume_cand is not None
                                and abs((t_found - resume_cand[0])
                                        - (t0_cap - resume_cand[1]))
                                <= 1.5)
                            lock_streak = lock_streak + 1 if coherent else 1
                            resume_cand = (t_found, t0_cap)
                        else:
                            lock_streak = 0
                            resume_cand = None
                    trace_add("R" if lock_streak else "P",
                              score if C is not None else None)
                    # a proven pause is free calibration: whatever energy
                    # arrives now is the streamer's talk alone - the live
                    # ceiling a reflex band must clear to ever arm
                    for b in (0, 1, 2):
                        r_ceil[b].append(self._rband(
                            block, audio_capture.CAPTURE_SR,
                            *TRK_R_BANDS[b]))
                        del r_ceil[b][:-300]
                        r_ceilv[b] = None
                    if (lock_streak >= 2 and not self.busy
                            and not self._session_running()):
                        player.sync_seek(t_found, t0_cap, self.offset)
                        self.q.put(("swap", False))
                        self.q.put(("status",
                                    "Auto: stream resumed - following."))
                        lock_streak = deg_streak = 0
                        resume_cand = None
                        r_try, r_tries, r_back = None, 0, 0
                        errs, classes, ref = [], [], None
                        meter("locked", score)
                        continue
                    now = time.monotonic()
                    if (now - wide_at >= TRK_SKIP_AFTER
                            and len(recent) >= 3 and not self.busy
                            and not self._session_running()):
                        # nothing at the pause point for a while - maybe
                        # the streamer skipped. One bounded, sure search.
                        wide_at = now
                        hit = self._wide_relock(recent, pause_point)
                        if hit is not None:
                            t_w, t0_w = hit
                            player.sync_seek(t_w, t0_w, self.offset)
                            self.q.put(("swap", False))
                            self.q.put((
                                "status",
                                "Auto: the stream moved - re-locked at "
                                f"{fmt_time(t_w)}."))
                            lock_streak = deg_streak = 0
                            resume_cand = None
                            r_try, r_tries, r_back = None, 0, 0
                            errs, classes, ref = [], [], None
                            meter("locked", 0.5)
                            continue
                    meter("paused")
            except Exception as e:
                check_failed = True
                sig = (type(e).__name__, str(e))
                fail_n = fail_n + 1 if sig == fail_sig else 1
                fail_sig = sig
                if fail_n == 1:
                    # first sight of this failure: could be a blip. Say
                    # so, keep the one full traceback, retry as usual.
                    self.q.put(("status", f"Auto-resync check failed: {e}"))
                    log.warning("auto loop error", exc_info=True)
                elif fail_n < TRK_FAIL_GIVEUP:
                    # same failure again: the traceback is already on
                    # file - one line keeps the log readable
                    log.warning("auto loop error repeated (%d/%d): %s",
                                fail_n, TRK_FAIL_GIVEUP, e)
                elif fail_n == TRK_FAIL_GIVEUP:
                    # not a blip - the drive unplugged, the film moved.
                    # One clear message, then disarm over the queue: the
                    # box unticks on the Tk thread, never from here.
                    log.warning("auto tracking gave up after %d "
                                "identical failures: %s: %s", fail_n, *sig)
                    if isinstance(e, (OSError, matcher.MatchError)):
                        why = ("can't read the film's audio - check the "
                               "file is still available")
                    else:
                        why = f"the same error kept hitting ({e})"
                    self.q.put(("auto_off",
                                f"Auto tracking stopped: {why}. "
                                "Re-tick Auto re-sync to try again."))
                # beyond the give-up: the untick is on its way through
                # the queue - idle on short sleeps until it lands
                time.sleep(1.0 if fail_n >= TRK_FAIL_GIVEUP
                           else max(self.auto_interval, 10))
            finally:
                if check_ran and not check_failed:
                    # a full check that ran clean breaks the streak -
                    # reflex-only chunk passes prove nothing about it
                    fail_sig, fail_n = None, 0
        close_listener()

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
        if not self.mute_var.get() and self.audio_device \
                and "blackhole" in self.audio_device.lower():
            # Local audio goes to the Multi-Output Device, which feeds
            # BlackHole - the very thing we listen to. Unmuted, a sync
            # would match the film against its own playback and lock onto
            # itself instead of the stream.
            self._set_status(
                "Warning: local audio is unmuted and reaches BlackHole - "
                "sync may match your own playback instead of the stream. "
                "Re-mute before syncing.")

    def _toggle_fullscreen(self):
        self._set_fullscreen(not self.fullscreen)

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
            if self.player is self.embedded:
                self._show_video_window()   # the session drives playback
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
            if self.player is self.embedded:
                self._show_video_window()   # the session drives playback
            self._set_leave_enabled(True)
            self._save_config()
            dlg.destroy()

        ttk.Button(frm, text="Join", command=start).grid(
            row=3, column=1, sticky="e", pady=(12, 0))
        dlg.grab_set()

    def _leave_session(self):
        if self.session is not None:
            # Closing the websocket runs a handshake that can sit for
            # seconds when the relay is unreachable - which is exactly when
            # someone reaches for Leave Session. The main thread also drives
            # libvlc's video output, so it cannot wait for that.
            sess, self.session = self.session, None
            threading.Thread(target=sess.stop, daemon=True).start()
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
        self._swap_app = ""          # resolve again against the new choice
        self._save_config()

    def _stream_swap(self, show):
        """Ask for the stream app to be shown (paused) or hidden (playing).

        Only the Tk half runs here - the osascript round-trips would block
        the main thread for seconds, so they happen on _swap_worker and come
        back through self.q as a "swapdone" event.
        """
        show = bool(show)
        if not self.swap_var.get() or not self.video_path \
                or show == self._swap_target:
            return
        self._swap_target = show
        self._swap_seq += 1
        if show and self.player is self.embedded and self.fullscreen:
            # Leave fullscreen before the browser is raised: a fullscreen Tk
            # window owns its own Space and the browser would come forward
            # behind it. The flag means "we owe the user fullscreen back",
            # so it is only ever set when we actually take it away - reading
            # self.fullscreen here would record False for a second pause
            # that arrives before the first one's restore has run, and the
            # debt would be forgotten.
            self._was_fullscreen = True
            self._set_fullscreen(False)
        self._swap_q.put((self._swap_seq, show,
                          self.player is self.embedded))

    def _resolve_stream_app(self):
        """Worker thread: name of the app to swap with, "" if there is none."""
        if self.stream_app:
            return self.stream_app
        if self._swap_app:
            return self._swap_app
        # "every application process" costs 0.5-1.5s, so hold on to the
        # answer. A miss is not cached, in case a browser opens later.
        running = macwindowctl.list_gui_apps()
        self._swap_app = next((b for b in BROWSERS if b in running), "")
        return self._swap_app

    def _swap_worker(self):
        while True:
            item = self._swap_q.get()
            # A rapid pause/resume only needs the state it settled on; drop
            # the swaps it passed through rather than play them back.
            while True:
                try:
                    item = self._swap_q.get_nowait()
                except queue.Empty:
                    break
            if item is None:
                return
            seq, show, embedded = item
            try:
                app_name = self._resolve_stream_app()
            except Exception:
                app_name = ""
            if not app_name:
                if show:
                    self.q.put(("status", "Pick the stream's browser under "
                                          "Advanced > Stream App first."))
                self.q.put(("swapdone", seq, show, False))
                continue
            try:
                if show:
                    macwindowctl.activate_app(app_name)
                elif embedded:
                    macwindowctl.hide_app(app_name)
                    macwindowctl.activate_self()
                else:
                    macwindowctl.hide_app(app_name)
                    macwindowctl.activate_app("VLC")
            except Exception as e:
                self._swap_app = ""  # that app may have quit - resolve again
                self.q.put(("status", f"App swap failed: {e} (grant Automation "
                                      "permission in System Settings > "
                                      "Privacy)."))
                self.q.put(("swapdone", seq, show, False))
                continue
            self.q.put(("swapdone", seq, show, True))

    def _swap_done(self, seq, show, ok):
        """Main thread: finish a swap the worker has applied."""
        if not ok:
            # Let the next pause try again instead of latching on a failure,
            # unless a newer swap is already on its way.
            if seq == self._swap_seq:
                self._swap_target = self._swapped
                if show and self._was_fullscreen \
                        and self.player is self.embedded:
                    # We left fullscreen to make way for a browser that
                    # never came up. Nothing raised it, so put the film
                    # back the way the user had it.
                    self._was_fullscreen = False
                    self._set_fullscreen(True)
            return
        self._swapped = show
        if not show and self.player is self.embedded:
            self._show_video_window()
            if self._was_fullscreen:
                self._was_fullscreen = False   # debt paid
                self._set_fullscreen(True)

    # ------------------------------------------------------------ subtitles

    def _refresh_subs(self):
        if self.player is not self.embedded:
            self._set_status("Use VLC.app's own Subtitles menu in external "
                             "mode.")
            return
        self._rebuild_subs_menu(self.embedded.subtitle_tracks())

    def _set_subtitle(self, tid):
        try:
            self.embedded.set_subtitle(tid)
        except Exception:
            pass

    def _load_sub_file(self):
        if self.player is not self.embedded:
            self._set_status("Load subtitles through VLC.app's menu in "
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
                if kind == "lock":
                    pass          # continuous stream; not worth log space
                elif kind in ("preview", "devices", "apps"):
                    log.debug("queue: %s", kind)      # payloads too bulky
                else:
                    log.debug("queue: %s %r", kind, payload)
                if kind == "lock":
                    lstate, strength = payload
                    if lstate != getattr(self, "_lock_state", None):
                        log.info("tracker: %s", lstate)
                        self._lock_state = lstate
                    self._draw_lock(lstate, strength)
                elif kind == "status":
                    self._set_status(payload[0])
                elif kind == "session":
                    self._set_status(payload[0])
                elif kind == "swap":
                    self._stream_swap(payload[0])
                elif kind == "swapdone":
                    self._swap_done(*payload)
                elif kind == "showroot":
                    self.root.deiconify()
                elif kind == "showvideo":
                    if self.player is self.embedded:
                        self._show_video_window()
                elif kind == "devices":
                    self._rebuild_device_menu(payload[0])
                    if not self.device_var.get() and payload[0]:
                        self.device_var.set(payload[0][0])
                        self._on_device_pick()
                elif kind == "apps":
                    self._rebuild_streamapp_menu(payload[0])
                elif kind == "preview":
                    self._show_preview(payload[0])
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
                elif kind == "auto_off":
                    # the tracker gave up (details in the log): untick
                    # exactly as a manual uncheck would, then say why
                    self.auto_var.set(False)
                    self._on_auto_toggle()
                    self._set_status(payload[0])
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

    def _draw_lock(self, state, strength):
        """The tracker's glanceable truth, next to the clock."""
        c = self.lock_canvas
        c.config(bg=self.root.cget("bg"))
        c.delete("all")
        if state == "locked":
            r = 3 + 3 * min(1.0, strength / 0.5)
            c.create_oval(8 - r, 7 - r, 8 + r, 7 + r,
                          fill="#2e7d32", outline="")
        elif state == "degraded":
            c.create_oval(2, 1, 14, 13, outline="#ef6c00", width=2)
        elif state == "paused":
            c.create_rectangle(4, 2, 7, 12, fill="#1565c0", outline="")
            c.create_rectangle(9, 2, 12, 12, fill="#1565c0", outline="")
        else:
            c.create_oval(6, 5, 10, 9, fill="#9e9e9e", outline="")

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
                "follow_pauses": self.follow_var.get(),
                "auto_resync": self.auto_var.get(),
                "reflex": self.reflex_mode,
                "swap": self.swap_var.get(),
                "stream_app": self.stream_app,
                "relay_url": self.relay_url,
            }))
            log.info("config saved (auto=%s follow=%s)",
                     self.auto_var.get(), self.follow_var.get())
        except OSError:
            # the log is the only witness: a save that fails silently on
            # exit looks identical to one that worked
            log.warning("config save FAILED", exc_info=True)

    def _load_config(self):
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
        except (OSError, ValueError):
            return
        path = cfg.get("video_path")
        if path and Path(path).is_file():
            self.video_path = path
            self.embedded.load(path)
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
        # renamed from "auto_follow": the old default force-wrote true on
        # every close, so an existing true can't be told apart from a
        # choice. New key = everyone starts from the new default (off)
        # exactly once; re-enabling sticks from then on.
        self.follow_var.set(bool(cfg.get("follow_pauses", False)))
        self.auto_follow = self.follow_var.get()
        # the master switch is persisted like every other setting - it
        # used to reset silently every launch, leaving "follow stream
        # pauses" checked, armed-looking, and completely inert
        mode = cfg.get("reflex", "live")
        self.reflex_mode = mode if mode in ("live", "shadow", "off") \
            else "live"
        if bool(cfg.get("auto_resync", False)):
            self.auto_var.set(True)
            self.auto_enabled = True
            # the menus don't exist yet: _build_menus reads auto_enabled
            # and enables the follow entry itself
            log.info("auto re-sync restored: live tracking armed"
                     + (", following pauses" if self.auto_follow else ""))
        self.swap_var.set(bool(cfg.get("swap", True)))
        self.stream_app = cfg.get("stream_app", "")
        if self.stream_app:
            self.streamapp_var.set(self.stream_app)
        if cfg.get("relay_url"):
            self.relay_url = cfg["relay_url"]

    def _on_close(self):
        self._closing = True
        self._swap_q.put(None)
        if self.session is not None:
            # Give the room teardown a moment to go out, but do not let a
            # dead relay hold the window on screen for its full close
            # timeout - quitting should feel immediate.
            closer = threading.Thread(target=self.session.stop, daemon=True)
            closer.start()
            closer.join(1.5)
        self._save_config()
        try:
            self.embedded.stop()
        except Exception:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    MacApp(root)
    if "--selftest" in sys.argv:
        root.after(3000, root.destroy)
    root.mainloop()
    if "--selftest" in sys.argv:
        print("SELFTEST OK")


if __name__ == "__main__":
    main()
