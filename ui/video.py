"""The film window: the built-in player's picture, its right-click menu,
the volume OSD and the fullscreen bar, plus WheelSteps.

FilmWindow is a mixin of the App (see ui/shell.py), so these methods use
the shell's state and actions through self. What differs between Windows
and the Mac comes from the platform adapter: whether the window takes the
mouse at all (FILM_CONTROLS), the menu buttons, the wheel's unit and
direction, whether the bar may show, and the hook that keeps a click on
the bar or the OSD from taking the keyboard off the film.

Mouse handlers defer their work to the next event-loop pass - see the
note in _build_video_window.
"""

import time
import tkinter as tk

from ui import theme, widgets

# The frame rate the +-1 frame buttons step by when libvlc cannot say
# (it reports 0 until a picture is out): a film's.
DEFAULT_FPS = 24.0

# How long after the bar or the OSD came or went the film takes the
# keyboard back from the main window (see FilmWindow._keep_film_keys):
# macOS hands it over 0.1 to 0.4 s after the change, and the check runs
# 50 ms after that. Kept short, as a move the user chose inside it, with
# no click to show it, is undone.
KEEP_KEYS_FOR = 0.75


class WheelSteps:
    """Wheel deltas into volume steps. A mouse notch is 120 on Windows; a
    precision touchpad, and every Mac trackpad, sends many small deltas
    instead (PR 31 review; Mac probe 2: 174 events, -8..2, in one slow
    finger-length).

    unit: the delta one step takes. sign: +1, or -1 to turn the deltas
    round (the Mac's natural scrolling), so that the fingers going up is
    louder either way; None counts as +1 and is filled in by the film
    window at the first wheel event. quiet: seconds without a wheel event
    after which the next one steps at once (None: never).

    Without a rest rule (Windows), a delta against the count so far
    starts it afresh: a notch back is a step back at once. With one (the
    Mac), the deltas are only added up: a trackpad's single gesture mixes
    signs (probe 2: -14 to 26 in one flick), and counting afresh at every
    small delta the other way would lose most of a slow scroll."""

    def __init__(self, unit, sign=None, quiet=None, clock=time.monotonic):
        self.unit, self.sign, self.quiet, self.clock = unit, sign, quiet, clock
        self.acc, self.last = 0, None

    def feed(self, delta):
        """The signed number of steps to take now for this event."""
        if not delta:
            return 0
        delta *= self.sign or 1
        now = self.clock()
        fresh = (self.quiet is not None
                 and (self.last is None or now - self.last > self.quiet))
        self.last = now
        if fresh:
            # A scroll starting from rest steps at once, so a mouse notch
            # (one event) is one step; what follows in the same gesture
            # adds up as below.
            self.acc = 0
            return 1 if delta > 0 else -1
        if self.quiet is None and self.acc * delta < 0:
            self.acc = 0             # turned back: count afresh
        self.acc += delta
        steps = int(self.acc / self.unit)       # toward zero, either sign
        self.acc -= steps * self.unit
        return steps


class FilmWindow:
    """The film window's builders and handlers."""

    FS_BAR_IDLE = 2.5        # seconds of a resting mouse before it hides

    # ----------------------------------------------------------- the window

    def _build_video_window(self):
        """The window the film renders into.

        The first Mac field test proved playback runs fine with no window
        at all: a sync matched, audio decoded, and there was simply
        nothing to look at, because libvlc's macOS output needs to be
        handed an NSView and had not been. So the app now owns the video
        window - a Tk frame whose NSView is passed to libvlc - and shows
        it when a film loads.
        """
        px = theme.px
        self._osd_win = None         # Tk-drawn volume OSD over the video
        self._osd_after = None
        # <Configure> reads this, and the bar is only built later
        self._fs_up = False
        # when the bar or the OSD last came or went over the fullscreen
        # film while it held the keyboard (see _keep_film_keys)
        self._overlay_at = None
        self.video_win = tk.Toplevel(self.root)
        self.video_win.title("StreamSync - video")
        w, h, at = self.FILM_GEOMETRY
        self.video_win.geometry(f"{px(w)}x{px(h)}"
                                + (f"+{px(at[0])}+{px(at[1])}" if at else ""))
        self.video_win.configure(bg="black")
        self.video_frame = tk.Frame(self.video_win, bg="black")
        self.video_frame.pack(fill="both", expand=True)
        self._bind_keys(self.video_win)
        # the bar follows the film when it moves or changes size (Windows'
        # fullscreen, a window moved by hand). The Mac sends no <Configure>
        # as the fullscreen animation ends: _fs_tick places it again there.
        self.video_win.bind("<Configure>", self._on_film_configure)
        if self.FOCUS_FILM_ON_FULLSCREEN:
            self.video_win.bind("<FocusOut>", self._on_film_focus_out)
            # a click in the main window is the user choosing it: the film
            # must not take the keyboard back from that
            self.root.bind("<ButtonPress>", self._on_main_press, add="+")
        # the sign is read at the first wheel event (the Mac asks macOS)
        self._wheel = WheelSteps(self.WHEEL_UNIT, sign=None,
                                 quiet=self.WHEEL_QUIET)
        if self.FILM_CONTROLS:
            # Clicks over the picture reach video_frame: with mouse input off,
            # libvlc creates its vout window disabled, so hit-testing passes it
            # by. That vout runs on libvlc's own thread with its input queue
            # attached to ours, so resizing the window (fullscreen) from inside
            # a mouse handler has deadlocked against it - every mouse handler
            # defers its work to the next event-loop pass. (On the Mac
            # libvlc's mouse input is on instead, and its view hands clicks
            # and the wheel on to Tk - see players.attach_tk.)
            self.video_win.bind("<Double-Button-1>", self._on_video_double)
            self.video_win.bind("<MouseWheel>", self._on_video_wheel)
            # Aqua sends sideways scrolling as Shift-wheel, which the plain
            # binding would otherwise take for the volume
            for seq in self.SIDEWAYS_WHEEL:
                self.video_win.bind(seq, lambda e: "break")
            # Aqua's right button is Button-2; a one-button mouse sends
            # Control-click
            for seq in self.MENU_BUTTONS:
                self.video_win.bind(seq, self._show_video_menu)
            self.video_win.bind("<Motion>", self._on_video_motion)
        # Closing the window hides it; the film (and the sync) carry on.
        self.video_win.protocol("WM_DELETE_WINDOW", self.video_win.withdraw)
        self.video_win.withdraw()
        # Windows: winfo_id must be real before the player attaches to it
        self.video_win.update_idletasks()

    def _on_film_focus_out(self, _event):
        # Not at once: the click that may have moved the keyboard reaches
        # the main window as an event of its own, maybe after this one.
        self.video_win.after(50, self._keep_film_keys)

    def _on_main_press(self, _event):
        self._overlay_at = None

    def _keep_film_keys(self):
        """The Mac: once the bar has come and gone over a fullscreen film,
        showing or hiding it again can make the main window the key window
        0.1 to 0.4 s later (measured on macOS 27 with Tk 8.6.16; the live
        test's E5 caught it). Keys then went to the main window, out of
        sight on another Space, and the film had lost the keyboard it was
        given when fullscreen settled. Give it back, once per change.

        macOS's move cannot be told from the user's own, so this is kept
        to the moment it happens: only if the film held the keyboard when
        the overlay came or went (_overlay_moved), only shortly after, only
        to the main window, and not once the user has clicked there. A
        dialog the user opened, or another app, keeps the keyboard. Taking
        it back raises the film, and switches to its Space."""
        at = self._overlay_at
        if (self._closing or at is None
                or time.monotonic() - at > KEEP_KEYS_FOR
                or not self._film_fullscreen()):
            return
        try:
            w = self.root.focus_get()
        except Exception:            # a widget path tkinter cannot resolve
            return
        if w is not None and w.winfo_toplevel() is self.root:
            self._overlay_at = None
            self.video_win.focus_set()

    def _overlay_moved(self):
        self._overlay_at = (time.monotonic()
                            if self._film_fullscreen() and self._film_has_keys()
                            else None)

    def _film_fullscreen(self):
        # What the window shows, not only self.fullscreen: on the Mac the
        # green button and Ctrl-Cmd-F leave fullscreen without telling the
        # app, and the flag only follows once something reads the window.
        return self.fullscreen and self._bar_allowed()

    def _film_has_keys(self):
        try:
            w = self.root.focus_get()
        except Exception:            # a widget path tkinter cannot resolve
            return False
        return w is not None and w.winfo_toplevel() is self.video_win

    def _hide_osd(self):
        self._osd_win.withdraw()
        self._overlay_moved()

    def _on_film_configure(self, event):
        # the toplevel's own, not video_frame's (its events reach this
        # binding too)
        if event.widget is self.video_win and self._fs_up:
            self.video_win.after_idle(self._place_fs_bar)

    # ------------------------------------------- video window mouse controls

    def _build_video_menu(self):
        m = tk.Menu(self.video_win, tearoff=0)
        m.add_command(label="Play / Pause", accelerator="Space",
                      command=self._toggle_pause)
        m.add_command(label="Fullscreen", accelerator="F",
                      command=self._toggle_fullscreen)
        m.add_separator()
        m.add_checkbutton(label="Mute film sound", variable=self.mute_var,
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
        self._sub_pick = None        # the ticked track, made with each listing

    def _on_video_double(self, _event):
        if self.player is not self.embedded:
            return          # external VLC has its own window to double-click
        self.video_win.after(1, self._toggle_fullscreen)

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
        self._fill_sub_menu(self._sub_menu)

    def _fill_sub_menu(self, menu):
        """List the subtitle tracks in `menu`, the current one ticked, then
        "Load subtitle file...". The film menu's Subtitles and the main
        window's Subtitles button both list them this way."""
        menu.delete(0, "end")
        current = self.embedded.current_subtitle()
        tracks = self.embedded.subtitle_tracks()
        self._sub_pick = tk.IntVar(value=current)
        if not tracks:
            menu.add_command(label="(none listed - start playback first)",
                             state="disabled")
        for tid, name in tracks:
            menu.add_radiobutton(
                label=name, value=tid, variable=self._sub_pick,
                command=lambda t=tid: self.embedded.set_subtitle(t))
        menu.add_separator()
        menu.add_command(label="Load subtitle file...",
                         command=self._load_sub_file)

    def _step_volume(self, step):
        v = self.embedded.volume()
        v = 100 if v is None else v
        v = max(0, min(125, v + step))
        self.embedded.set_volume(v)
        # never unmutes: "Mute film sound" is load-bearing while streaming
        # (loopback must not hear the film), so an accidental scroll must
        # not defeat it the way stock VLC's wheel would.
        self._flash_osd(f"Volume {v}%" + (" (muted)" if self.mute_var.get()
                                          else ""))

    def _flash_osd(self, text, ms=900):
        """VLC-style OSD drawn by Tk, floated over the video's top-right.
        (libvlc's own marquee cannot be driven from the thread hosting the
        video window - see the note in players.py.)"""
        px = theme.px
        if self._osd_win is None:
            self._osd_win = tk.Toplevel(self.video_win)
            # every window of ours is titled "StreamSync...": that is how
            # the Windows stream-window list leaves them out
            self._osd_win.title("StreamSync - volume")
            self._osd_win.overrideredirect(True)
            self._osd_win.attributes("-topmost", True)
            self._osd_lbl = tk.Label(self._osd_win, bg=theme.OSD_BG,
                                     fg=theme.OSD_FG, padx=px(14), pady=px(6),
                                     bd=0, highlightthickness=0,
                                     font=theme.font(17, bold=True))
            self._osd_lbl.pack()
            self._osd_win.withdraw()
            self._osd_hook = None    # its no-activate hook, kept alive
            self._osd_hwnd = None    # the OS frame that hook is on
        self._osd_lbl.config(text=text)
        self._osd_win.update_idletasks()
        x = (self.video_win.winfo_rootx() + self.video_win.winfo_width()
             - self._osd_win.winfo_reqwidth() - px(24))
        y = self.video_win.winfo_rooty() + px(24)
        self._osd_win.geometry(f"+{x}+{y}")
        self._osd_win.deiconify()
        self._overlay_moved()
        # a click on it must leave the film the active window, as for the
        # fullscreen bar (see windowctl.no_activate_on_click)
        self._osd_win.update_idletasks()
        self._osd_hook, self._osd_hwnd = self._no_activate(
            self._osd_win, self._osd_hook, self._osd_hwnd)
        if self._osd_after is not None:
            self.video_win.after_cancel(self._osd_after)
        self._osd_after = self.video_win.after(ms, self._hide_osd)

    def _on_video_wheel(self, event):
        if self.player is not self.embedded or not event.delta:
            return
        # A mouse notch is 120; a precision touchpad sends many small
        # deltas instead. Step 5 per 120 they add up to, so a short swipe
        # is not a dozen steps. (WheelSteps counts them, with the Mac's
        # unit and rest where the platform sets them. Sideways scrolling
        # never gets here on the Mac: SIDEWAYS_WHEEL swallows it.)
        if self._wheel.sign is None:
            # the Mac's `defaults read` runs once, at the first scroll
            self._wheel.sign = self._wheel_sign()
        steps = self._wheel.feed(event.delta)
        if steps:
            self.video_win.after(1, self._step_volume, 5 * steps)

    # ---------------------------------------------- entering fullscreen

    def _fullscreen_entered(self):
        """The film is fullscreen now: Windows at once, the Mac once
        _settle_fullscreen sees it."""
        if self.FOCUS_FILM_ON_FULLSCREEN:
            # Mac: entering fullscreen didn't make the film's window key
            # (probe 2), so Space, F and Esc over it would go nowhere
            self.video_win.after(1, self.video_win.focus_force)
        if self.OVERLAYS_PER_FULLSCREEN:
            self._rebuild_overlays()
        if self.FILM_CONTROLS:
            # A mouse resting over the film brings no motion with it
            # (Windows only repeats the last position, which
            # _on_video_motion ignores), so the controls start their rest
            # here: they show, then go with the pointer as usual.
            self._fs_moved = time.monotonic()
            self.video_win.after(1, self._show_fs_bar)

    def _rebuild_overlays(self):
        """Make the bar and the volume overlay again, on the Space the
        fullscreen film is on now. Built once at startup, the bar showed
        on the desktop's Space, behind the film; the overlay probes, which
        built their bar after entering fullscreen, never met this. What the
        rest of the app has told the bar is carried over."""
        if self._fs_up:
            self._hide_fs_bar()
        if self._fs_after is not None:
            self.video_win.after_cancel(self._fs_after)
        status = self._fs_status.cget("text")
        states = [b.cget("state") for b in (self._fs_resync, self._fs_full)]
        # the pointer's last place and time too: a move repeating it is
        # still not a move
        kept = (self._fs_hold, self._fs_xy, self._fs_moved, self._fs_pos)
        self.fs_bar.destroy()
        self._build_fs_bar()
        self._fs_status.config(text=status)
        for b, state in zip((self._fs_resync, self._fs_full), states):
            b.config(state=state)
        self._fs_hold, self._fs_xy, self._fs_moved, self._fs_pos = kept
        if self._osd_win is not None:
            if self._osd_after is not None:
                self.video_win.after_cancel(self._osd_after)
                self._osd_after = None
            self._osd_win.destroy()
            self._osd_win = None      # _flash_osd makes it again when needed

    # ------------------------------------------------ fullscreen control bar

    def _build_fs_bar(self):
        """VLC-style controls over the bottom of a fullscreen film: they
        appear when the mouse moves and hide, with the pointer, once it
        rests. A top-level window of their own, like VLC's (and like the
        volume OSD): a widget inside the video window would have to sit
        over libvlc's Direct3D child window."""
        px, font = theme.px, theme.font
        bg, fg = theme.BAR_BG, theme.BAR_FG
        bar = tk.Toplevel(self.video_win, bg=bg, highlightthickness=1,
                          highlightbackground=theme.BAR_LINE)
        # every window of ours is titled "StreamSync...": that is how the
        # Windows stream-window list leaves them out
        bar.title("StreamSync - controls")
        bar.overrideredirect(True)
        bar.attributes("-topmost", True)
        bar.attributes("-alpha", theme.BAR_ALPHA)
        bar.withdraw()
        row = tk.Frame(bar, bg=bg)
        row.pack(fill="x", padx=px(18), pady=(px(14), px(5)))
        # no padding of a label's own (3 px each way): the board's gaps
        tight = {"bd": 0, "padx": 0, "pady": 0, "highlightthickness": 0}
        self._fs_time = tk.Label(row, text="-:--", width=8, anchor="e",
                                 bg=bg, fg=fg, font=font(14), **tight)
        self._fs_time.pack(side="left")
        self._fs_track = widgets.Timeline(
            row, bg=bg, rest=theme.BAR_TRACK, played=theme.ACCENT,
            knob=theme.KNOB, knob_r=7, thickness=5, width=40,
            can_seek=self._fs_can_seek, on_preview=self._fs_preview,
            on_seek=self._fs_seek)
        self._fs_track.pack(side="left", fill="x", expand=True, padx=px(14))
        self._fs_len = tk.Label(row, text="-:--", width=8, anchor="w",
                                bg=bg, fg=fg, font=font(14), **tight)
        self._fs_len.pack(side="left")
        # the board's 64 px for each time; a label's width counts in its
        # font's "0"s
        try:
            zero = int(row.tk.call("font", "measure", font(14), "-displayof", row, "0"))
            for lbl in (self._fs_time, self._fs_len):
                lbl.config(width=-(-px(64) // zero))
        except Exception:
            pass                     # a stand-in Tk (tests) has no fonts
        row = tk.Frame(bar, bg=bg)
        row.pack(fill="x", padx=px(18), pady=(px(5), px(14)))
        self._fs_play = self._fs_button(row, self._toggle_pause,
                                        "Pause", "pause")
        self._fs_stop = self._fs_button(row, self._fs_stop_clicked,
                                        "Stop", "stop")
        self._fs_separator(row)
        self._fs_frame_back = self._fs_button(
            row, lambda: self._fs_frame_step(-1), "−1 frame",
            "frame_back")
        self._fs_frame_fwd = self._fs_button(
            row, lambda: self._fs_frame_step(+1), "+1 frame", "frame_fwd",
            icon_after=True)
        self._fs_separator(row)
        self._fs_resync = self._fs_button(row, self._fs_resync_clicked,
                                          "Resync here", "resync")
        self._fs_full = self._fs_button(
            row, lambda: self._fs_resync_clicked(whole=True),
            "Resync (full)", "resync")
        # width=1: a long status is cut off, not a reason to widen the bar
        self._fs_status = tk.Label(row, width=1, anchor="w", bg=bg,
                                   fg=theme.BAR_MUTED, font=font(12), **tight)
        self._fs_status.pack(side="left", fill="x", expand=True,
                             padx=(px(10), 0))
        self._bind_keys(bar)
        self.fs_bar = bar
        self._fs_hook = None         # its no-activate hook, kept alive
        self._fs_hwnd = None         # the OS frame that hook is on
        self._fs_up = False
        self._fs_after = None
        self._fs_xy = None           # last pointer position over the film
        self._fs_moved = 0.0         # ...and when it got there
        self._fs_hold = False        # a resync from the bar is running
        self._fs_pos = (None, None)  # (time, length) last read
        self._fs_placed = None       # the film's box the bar was placed on

    def _fs_button(self, parent, action, text, icon, icon_after=False):
        # deferred like every mouse handler over the film (see
        # _build_video_window): Play/Pause can resize the video window.
        # FlatButton runs its command on the next event-loop pass.
        b = widgets.FlatButton(parent, text, icon=icon, icon_after=icon_after,
                               kind="bar", height=34, font=theme.font(14),
                               command=action, takefocus=False)
        b.pack(side="left", padx=(0, theme.px(6)))
        return b

    def _fs_separator(self, parent):
        px = theme.px
        tk.Frame(parent, width=px(1), height=px(22),
                 bg=theme.BAR_SEP).pack(side="left", padx=px(6))

    def _on_video_motion(self, event):
        xy = (event.x_root, event.y_root)
        if xy == self._fs_xy:
            # Windows repeats the last position when a window appears or
            # vanishes under a resting pointer - the bar hiding, for one
            return
        self._fs_xy = xy
        if self.video_win.cget("cursor"):
            # A real move brings back a pointer that went with the bar,
            # whatever the bar does next: if the film left fullscreen
            # behind the app's back meanwhile (the Mac's green button,
            # Ctrl-Cmd-F), the bar does not come back to show it.
            self._set_film_cursor("")
        if not self.fullscreen or self.player is not self.embedded:
            return
        self._fs_moved = time.monotonic()
        if not self._fs_up:
            self.video_win.after(1, self._show_fs_bar)

    def _show_fs_bar(self):
        if (self._closing or self._fs_up or not self.fullscreen
                or self.player is not self.embedded
                or not self.FILM_CONTROLS):
            return
        if not self._bar_allowed():
            self._fullscreen_from_window()   # see _fs_tick
            return
        win, bar = self.video_win, self.fs_bar
        self._set_film_cursor("")
        self._place_fs_bar()
        self._fs_up = True
        self._fs_refresh()
        bar.deiconify()
        bar.update_idletasks()
        self._overlay_moved()
        self._fs_hook, self._fs_hwnd = self._no_activate(
            bar, self._fs_hook, self._fs_hwnd)
        self._fs_after = win.after(250, self._fs_tick)

    def _place_fs_bar(self):
        win, bar = self.video_win, self.fs_bar
        bar.update_idletasks()
        # 1100 px at 100% scaling and more as the DPI grows, never narrower
        # than the controls ask for, nor wider than the (fullscreen) film
        scale = self._ui_scale()
        w = max(bar.winfo_reqwidth(),
                min(round(1100 * scale), win.winfo_width() - 80))
        w = min(w, win.winfo_width())
        h = bar.winfo_reqheight()
        x = win.winfo_rootx() + (win.winfo_width() - w) // 2
        y = win.winfo_rooty() + win.winfo_height() - h - 40
        bar.geometry(f"{w}x{h}+{x}+{y}")
        self._fs_placed = self._film_box()

    def _film_box(self):
        win = self.video_win
        return (win.winfo_rootx(), win.winfo_rooty(),
                win.winfo_width(), win.winfo_height())

    def _fs_tick(self):
        self._fs_after = None
        if not self._fs_up:
            return
        allowed = self._bar_allowed()
        if not allowed:
            # The window may have left fullscreen behind the app's back
            # (the Mac's green button, Ctrl-Cmd-F): self.fullscreen follows
            # it now, so nothing goes on treating the film as fullscreen.
            self._fullscreen_from_window()
        if (not self.fullscreen or self.player is not self.embedded
                or not self.video_win.winfo_viewable() or not allowed):
            self._hide_fs_bar()
            return
        # A resync from the bar holds it up until the result is in, where
        # the platform lets the hold stand (see _bar_hold_stands).
        held = self._fs_hold and self._bar_hold_stands()
        if (time.monotonic() - self._fs_moved >= self.FS_BAR_IDLE
                and not (self._fs_track.dragging or held
                         or self._pointer_on_fs_bar())):
            self._hide_fs_bar(hide_pointer=True)
            return
        if self._film_box() != self._fs_placed:
            # The film moved with no <Configure> to say so: on the Mac
            # (macOS 27, Tk 8.6.16) the window has its full size as soon as
            # it is fullscreen, but sits 28 px down, under the menu bar,
            # until the animation ends about 0.7 s in - after the bar can
            # first show.
            self._place_fs_bar()
        self._fs_refresh()
        self._fs_after = self.video_win.after(250, self._fs_tick)

    def _hide_fs_bar(self, hide_pointer=False):
        if self._fs_after is not None:
            self.video_win.after_cancel(self._fs_after)
            self._fs_after = None
        self._fs_up = False
        self._fs_track.cancel_drag()
        self.fs_bar.withdraw()
        self._overlay_moved()
        # as in VLC, a resting pointer goes with the controls
        self._set_film_cursor("none" if hide_pointer else "")

    def _set_film_cursor(self, cursor):
        for w in (self.video_win, self.video_frame):
            w.config(cursor=cursor)

    def _pointer_on_fs_bar(self):
        bar = self.fs_bar
        x, y = bar.winfo_pointerxy()
        bx, by = bar.winfo_rootx(), bar.winfo_rooty()
        return (bx <= x < bx + bar.winfo_width()
                and by <= y < by + bar.winfo_height())

    def _fs_refresh(self):
        p = self.player
        try:
            t, n, playing = p.time(), p.length(), p.is_playing()
        except Exception:
            return
        self._fs_pos = (t, n)
        play = ("Pause", "pause") if playing else ("Play", "play")
        if self._fs_play.cget("text") != play[0]:
            self._fs_play.config(text=play[0], icon=play[1])
        self._fs_len.config(text=widgets.fmt_clock(n) if n else "-:--")
        if not self._fs_track.dragging:
            self._fs_time.config(
                text="-:--" if t is None else widgets.fmt_clock(t))
            self._fs_track.set(min(1.0, max(0.0, t / n))
                               if t is not None and n else None)

    def _fs_can_seek(self):
        t, n = self._fs_pos
        # stopped or never started: nothing to seek in
        return t is not None and bool(n)

    def _fs_preview(self, frac):
        n = self._fs_pos[1]
        if n:
            self._fs_time.config(text=widgets.fmt_clock(frac * n))

    def _fs_seek(self, frac):
        # the knob follows the drag; the film moves once, on release
        # (Timeline has deferred this to the next event-loop pass)
        n = self._fs_pos[1]
        if not n:
            return          # stopped since the press: nothing to seek in
        self.player.seek(frac * n)
        self._mark_moved()

    def _fs_stop_clicked(self):
        self.player.stop()
        self._fs_refresh()

    def _fs_resync_clicked(self, whole=False):
        """Resync around the film's position, or (whole) search the whole
        film's audio - even with a position hint set."""
        started = self._sync(whole=True) if whole else self._resync()
        if started:
            # stay up until the result is in: the bar shows the status.
            # (Not when another sync was already running: this click
            # started nothing, and that one is not the bar's to report.)
            self._fs_hold = True
            (self._fs_full if whole else self._fs_resync).config(
                text="Resyncing...")

    def _fs_sync_done(self):
        self._fs_resync.config(state="normal", text="Resync here")
        self._fs_full.config(state="normal", text="Resync (full)")
        if self._fs_hold:
            self._fs_hold = False
            self._fs_moved = time.monotonic()   # then the usual time to read it

    def _fs_frame_step(self, direction):
        """One frame back or on, as a nudge: the offset remembers it, like
        Fine-tune. Paused, the player steps exactly (EmbeddedPlayer.nudge);
        playing, it is a rate trim of the same size."""
        self._nudge(direction / self._film_fps())

    def _film_fps(self):
        try:
            f = self.embedded.fps()
        except Exception:
            f = None
        return f if f and 1 <= f <= 240 else DEFAULT_FPS
