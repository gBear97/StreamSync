"""The main window: the FirstRun and Main boards, and their view methods.

_build_main builds both boards once and shows FirstRun; _show_main_state
swaps them when a film comes and goes. Everything else here is a view: it
changes widgets, never ctl or the settings, so the Shell decides what
happened and a view only says it. VIEWS lists them, so a test can stub
them all.

Run `python -m ui.mainwin` for a gallery of both boards over a stand-in
app, at 100% and (in a second process) at 150%.
"""

import sys
import tkinter as tk
from types import SimpleNamespace

from ui import theme, widgets
from version import __version__

# The boards' window is 560 wide with 26 px either side, and the cards add
# a 1-px border and 18 px of padding: what a card's content gets, in
# design px. The controls row is fitted to it and the status line wraps at
# it, so neither can make the window grow.
CONTENT_W = 560 - 2 * 26
CARD_INNER_W = CONTENT_W - 2 * 1 - 2 * 18
# The board spaces the playback buttons 8 apart. Tk's fonts run a little
# wider than the board's, and at 8 "Film sound on" with "Subtitles: in VLC"
# overflows the card by a few px; at 6 every fixed text fits whole, and only
# a long track name is cut.
CONTROLS_GAP = 6

# A tk.Label pads its text by 3 px each way (a 2-px border and 1 px of
# padding), which the boards' text does not have: the gaps between texts
# are the layout's, so every label here is made without them.
_TIGHT = {"bd": 0, "padx": 0, "pady": 0, "highlightthickness": 0}

# state -> (dot colour token, strong text, rest; "{at}" is the stream position)
SYNC_STATES = {
    "none": ("FAINT", "Not synced yet", ""),
    "syncing": ("ACCENT", "Syncing", "…"),
    "synced": ("SYNCED", "Synced", " - stream at {at}"),
    "weak": ("WARN", "Weak match", " - first guess at {at}"),
    "missed": ("ERROR", "No match", " - the film was not moved"),
    "failed": ("ERROR", "Sync failed", ""),
    "moved": ("FAINT", "Moved by hand", " - Resync to line up again"),
}
# Only a match that moved the film has a time worth counting from.
AGO_STATES = ("synced", "weak")


def _measure(widget, font, text):
    """`text`'s width in screen px in `font`, on `widget`'s display."""
    return int(widget.tk.call("font", "measure", font, "-displayof", widget, text))


class MainWindow:
    """The main window's builder and views. A view changes widgets only,
    never ctl or the settings.

    Public widgets, for the Shell and the tests: file_lbl, status_lbl (a
    tk.Label), sync_btn, resync_btn, offset_lbl, timeline, play_btn,
    mute_btn, subs_btn, party_btn, film_link; and the two boards,
    first_frame and main_frame."""

    VIEWS = ("_show_main_state", "_show_film", "_show_film_link", "_show_sync_state",
             "_show_ago", "_show_auto_sub", "_show_position", "_preview_position",
             "_show_mute", "_show_subtitle", "_show_offset", "_show_party_live",
             "_show_listen", "_show_status_line")

    def _build_main(self):
        px = theme.px
        # Never more than the screen allows a window. On a small screen
        # (CI's 1024x768, or a 13-inch Mac set to "Larger Text") a minimum
        # taller than that maximum left Tk at its 200x200 default, with
        # most of the board cut off.
        try:
            maxw, maxh = (int(v) for v in self.root.wm_maxsize())
        except (TypeError, ValueError, AttributeError, tk.TclError):
            maxw = maxh = 1 << 20    # a stand-in Tk (tests) has no screen
        self.root.minsize(min(px(560), maxw), min(px(664), maxh))
        outer = tk.Frame(self.root, bg=theme.GROUND)
        outer.pack(fill="both", expand=True, padx=px(26), pady=(px(22), px(20)))

        # The footer is packed first, at the bottom, so it keeps its place
        # whichever board is packed above it.
        footer = self._build_footer(outer)
        self.first_frame = tk.Frame(outer, bg=theme.GROUND)
        self._build_first_run(self.first_frame)
        self.main_frame = tk.Frame(outer, bg=theme.GROUND)
        self._build_main_board(self.main_frame)
        self.first_frame.pack(fill="both", expand=True)
        # Made first, the footer would also be first in the Tab order:
        # Settings and Watch party come after the board, where they sit.
        widgets.tab_last(footer)

        # One status line for both boards: under the sync-state row on Main,
        # and under the listening line on FirstRun, where a film that could
        # not be reopened says why. Its parent is the frame both boards sit
        # in, so it can be packed into either; made after them, it stacks
        # above them.
        self.status_lbl = tk.Label(outer, text="", font=theme.font(12), fg=theme.MUTED,
                                   bg=theme.CARD, anchor="nw", justify="left", bd=0,
                                   padx=0, pady=0, highlightthickness=0)
        self._status_shown = False
        self._in_main = False

        self._show_sync_state("none")
        self._show_auto_sub()
        self._show_position(None, None, False)
        self._show_mute()
        self._show_subtitle("Off")
        self._show_offset(0.0)
        self._show_party_live(False)
        self._show_listen(self._device_label(self.device_var.get()))

    def _build_footer(self, outer):
        px, font = theme.px, theme.font
        footer = tk.Frame(outer, bg=theme.GROUND)
        footer.pack(side="bottom", fill="x", pady=(px(18), 0))
        widgets.FlatButton(footer, "Settings", icon="settings", kind="ghost", height=36,
                           font=font(14),
                           command=lambda: self._open_settings()).pack(side="left")
        self.party_btn = widgets.FlatButton(footer, "Watch party", icon="people", kind="ghost",
                                            height=36, font=font(14),
                                            command=lambda: self._open_watch_party())
        self.party_btn.pack(side="left", padx=(px(8), 0))
        # The board has no session state; a dot beside the button says one
        # is running, since its window may be closed.
        self._party_dot = widgets.Dot(footer, theme.SYNCED, size=9)
        tk.Label(footer, text=f"StreamSync {__version__}", font=font(12), bg=theme.GROUND,
                 fg=theme.FAINT, **_TIGHT).pack(side="right")
        return footer

    def _build_first_run(self, first):
        px, font = theme.px, theme.font
        ground, card_bg = theme.GROUND, theme.CARD

        card = widgets.Card(first, padding=28, radius=10)
        card.pack(fill="both", expand=True)
        # The card takes the board's spare height; this keeps its content
        # centred in it.
        hero = tk.Frame(card.body, bg=card_bg)
        hero.pack(expand=True)
        widgets.IconTile(hero, "film", 64, radius=14, fg=theme.ACCENT).pack()
        tk.Label(hero, text="Open the film you're watching along to", font=font(22, True),
                 bg=card_bg, fg=theme.TEXT, justify="center",
                 wraplength=px(CONTENT_W - 2 - 2 * 28), **_TIGHT).pack(pady=(px(16), 0))
        tk.Label(hero, text="Your own copy, on this computer.", font=font(14), bg=card_bg,
                 fg=theme.MUTED, justify="center", wraplength=px(380),
                 **_TIGHT).pack(pady=(px(16), 0))
        widgets.FlatButton(hero, "Open film…", kind="primary", height=46, font=font(16, True),
                           padx=26,
                           command=lambda: self._choose_file()).pack(pady=(px(16), 0))

        steps = tk.Frame(first, bg=ground)
        steps.pack(fill="x", pady=(px(18), 0))
        text_w = CONTENT_W - 26 - 12
        for n, text in ((1, "Open your copy of the film."),
                        (2, "Play the stream in your browser, with its sound on."),
                        (3, None)):
            row = tk.Frame(steps, bg=ground)
            row.pack(fill="x", pady=(px(12) if n > 1 else 0, 0))
            d = px(26)
            circle = tk.Canvas(row, width=d, height=d, bg=ground, highlightthickness=0, bd=0)
            circle.create_oval(0, 0, d, d, fill=theme.RAISED, outline="")
            circle.create_text(d // 2, d // 2, text=str(n), font=font(13, True), fill=theme.TEXT)
            circle.pack(side="left")
            if text:
                tk.Label(row, text=text, font=font(14), bg=ground, fg=theme.TEXT,
                         anchor="w", justify="left", **_TIGHT).pack(side="left", padx=(px(12), 0))
                continue
            rich = widgets.RichText(row, [("Press ", None), ("Sync to stream", "bold"),
                                          (". StreamSync listens and jumps the film to the "
                                           "same moment.", None)],
                                    width=text_w, font=font(14))
            # A click would give this Text the keyboard on Windows, and the
            # keys ignore a focused text widget (Shell._typing).
            rich.bind("<Button-1>", lambda e: "break")
            rich.pack(side="left", fill="x", expand=True, padx=(px(12), 0))

        # Disabled for as long as this board shows: there is nothing to sync
        # yet. Disabled also takes it out of the Tab order.
        self._first_sync_btn = widgets.FlatButton(first, "Sync to stream", kind="primary",
                                                  height=48, font=font(16, True),
                                                  state="disabled")
        self._first_sync_btn.pack(fill="x", pady=(px(18), 0))

        self._listen_row = tk.Frame(first, bg=ground)
        self._listen_row.pack(fill="x", pady=(px(18), 0))
        self._listen_lbl = tk.Label(self._listen_row, text="", font=font(13), bg=ground,
                                    fg=theme.MUTED, **_TIGHT)
        self._listen_lbl.pack(side="left")
        # padx 0: the board's link sits in the sentence
        self._listen_link = widgets.FlatButton(self._listen_row, "change", kind="link",
                                               height=18, font=font(13), padx=0,
                                               underline=True,
                                               command=lambda: self._open_settings())
        self._listen_link.pack(side="left")

    def _build_main_board(self, main):
        px, font = theme.px, theme.font
        ground, card_bg = theme.GROUND, theme.CARD

        # header: the film, what plays it, and another film
        header = tk.Frame(main, bg=ground)
        header.pack(fill="x")
        self._film_tile = widgets.IconTile(header, "film", 44, radius=8, fg=theme.MUTED,
                                           cursor="hand2",
                                           command=lambda: self._film_tile_clicked())
        self._film_tile.pack(side="left")
        opener = widgets.FlatButton(header, "Open film…", kind="secondary", height=36,
                                    font=font(14), padx=14, command=lambda: self._choose_file())
        opener.pack(side="right")
        title = tk.Frame(header, bg=ground)
        title.pack(side="left", fill="x", expand=True, padx=px(14))
        # packed before the title so that the title takes only the room
        # left, and reached after the title's "Show film window" link
        widgets.tab_last(opener)
        # width=1: the name never asks for room, so a long one cannot widen
        # the window; it is cut to the width it is given instead.
        self._film_name = ""
        self.file_lbl = tk.Label(title, text="", width=1, anchor="w", font=font(16, True),
                                 bg=ground, fg=theme.TEXT, **_TIGHT)
        self.file_lbl.pack(fill="x")
        self.file_lbl.bind("<Configure>", lambda e: self._fit_film_name(e.width), add="+")
        info = tk.Frame(title, bg=ground)
        info.pack(fill="x", pady=(px(2), 0))
        self._film_info = tk.Label(info, text="", font=font(13), bg=ground, fg=theme.MUTED,
                                   **_TIGHT)
        self._film_info.pack(side="left")
        self.film_link = widgets.FlatButton(info, "Show film window", kind="link", height=18,
                                            font=font(13), padx=0, underline=True,
                                            command=lambda: self._show_video_window())

        # sync card
        sync = widgets.Card(main, padding=18)
        sync.pack(fill="x", pady=(px(18), 0))
        buttons = tk.Frame(sync.body, bg=card_bg)
        buttons.pack(fill="x")
        self.sync_btn = widgets.FlatButton(buttons, "Sync to stream", kind="primary",
                                           height=48, font=font(16, True),
                                           command=lambda: self._sync())
        self.sync_btn.pack(side="left", fill="x", expand=True)
        self.resync_btn = widgets.FlatButton(buttons, "Resync", icon="resync", kind="secondary",
                                             height=48, font=font(15), padx=18,
                                             command=lambda: self._resync())
        self.resync_btn.pack(side="left", padx=(px(10), 0))

        self._state_row = tk.Frame(sync.body, bg=card_bg)
        self._state_row.pack(fill="x", pady=(px(14), 0))
        self._sync_dot = widgets.Dot(self._state_row, theme.FAINT, size=9)
        self._sync_dot.pack(side="left")
        self._sync_strong = tk.Label(self._state_row, text="", font=font(14, True), bg=card_bg,
                                     fg=theme.TEXT, **_TIGHT)
        self._sync_strong.pack(side="left", padx=(px(10), 0))
        self._sync_rest = tk.Label(self._state_row, text="", font=font(14), bg=card_bg,
                                   fg=theme.TEXT, **_TIGHT)
        self._sync_rest.pack(side="left")
        self._ago_lbl = tk.Label(self._state_row, text="", font=font(13), bg=card_bg,
                                 fg=theme.MUTED, **_TIGHT)
        self._ago_lbl.pack(side="right")

        auto = tk.Frame(sync.body, bg=card_bg)
        auto.pack(fill="x", pady=(px(14), 0))
        widgets.Switch(auto, self.auto_var,
                       command=lambda: self._on_auto_toggle()).pack(side="left")
        words = tk.Frame(auto, bg=card_bg)
        words.pack(side="left", fill="x", expand=True, padx=(px(12), 0))
        tk.Label(words, text="Keep in sync automatically", font=font(14), bg=card_bg,
                 fg=theme.TEXT, anchor="w", **_TIGHT).pack(fill="x")
        self._auto_sub = tk.Label(words, text="", font=font(12), bg=card_bg, fg=theme.MUTED,
                                  anchor="w", **_TIGHT)
        self._auto_sub.pack(fill="x", pady=(px(1), 0))

        # playback card
        play = widgets.Card(main, padding=18)
        play.pack(fill="x", pady=(px(18), 0))
        times = tk.Frame(play.body, bg=card_bg)
        times.pack(fill="x")
        self._pos_lbl = tk.Label(times, text="0:00", width=7, anchor="e", font=font(13),
                                 bg=card_bg, fg=theme.TEXT, **_TIGHT)
        self._pos_lbl.pack(side="left")
        self._len_lbl = tk.Label(times, text="-:--", width=7, anchor="w", font=font(13),
                                 bg=card_bg, fg=theme.MUTED, **_TIGHT)
        self._len_lbl.pack(side="right")
        # Room for h:mm:ss and no more, as on the board. A fixed width keeps
        # the track still as the time ticks, in a font whose digits are not
        # all one width; a label's width counts in its font's "0"s.
        try:
            f = font(13)
            zeros = -(-_measure(times, f, "0:00:00") // _measure(times, f, "0"))
            for lbl in (self._pos_lbl, self._len_lbl):
                lbl.config(width=zeros)
        except Exception:
            pass                 # a stand-in Tk (tests) has no fonts
        self.timeline = widgets.Timeline(times, bg=card_bg, rest=theme.LINE,
                                         played=theme.ACCENT, knob=theme.KNOB, knob_r=8,
                                         thickness=6,
                                         can_seek=lambda: self._can_seek(),
                                         on_preview=lambda frac: self._preview_position(frac),
                                         on_seek=lambda frac: self._seek_to_frac(frac))
        self.timeline.pack(side="left", fill="x", expand=True, padx=px(12))

        self._controls = controls = tk.Frame(play.body, bg=card_bg)
        controls.pack(fill="x", pady=(px(14), 0))
        self.play_btn = widgets.FlatButton(controls, kind="round", icon="play", height=44,
                                           command=lambda: self._toggle_pause())
        self.play_btn.pack(side="left")
        self._full_btn = widgets.FlatButton(controls, "Fullscreen", icon="fullscreen",
                                            kind="secondary", height=36, font=font(14),
                                            command=lambda: self._toggle_fullscreen())
        self._full_btn.pack(side="left", padx=(px(CONTROLS_GAP), 0))
        self.mute_btn = widgets.FlatButton(controls, "Film muted", icon="speaker_muted",
                                           kind="secondary", height=36, font=font(14),
                                           command=lambda: self._toggle_mute())
        self.mute_btn.pack(side="left", padx=(px(CONTROLS_GAP), 0))
        self._subs_text = "Off"
        self.subs_btn = widgets.FlatButton(controls, "Subtitles: Off", icon="subtitles",
                                           kind="secondary", height=36, font=font(14),
                                           command=lambda: self._open_subs_menu())
        self.subs_btn.pack(side="left", padx=(px(CONTROLS_GAP), 0))

        widgets.Divider(play.body).pack(fill="x", pady=(px(14), 0))
        fine = tk.Frame(play.body, bg=card_bg)
        fine.pack(fill="x", pady=(px(12), 0))
        tk.Label(fine, text="Fine-tune", font=font(13), bg=card_bg,
                 fg=theme.MUTED, **_TIGHT).pack(side="left", padx=(0, px(6)))
        for delta in (-0.5, -0.1, None, 0.1, 0.5):
            if delta is None:
                # The board's 64 px at least, with room for "+10.00 s", so
                # the chips do not shift as the offset changes. A label's
                # width counts in its font's "0"s.
                self.offset_lbl = tk.Label(fine, text="+0.00 s", font=font(14, True),
                                           bg=card_bg, fg=theme.TEXT, **_TIGHT)
                try:
                    f = font(14, True)
                    need = max(px(64), _measure(self.offset_lbl, f, "+10.00 s"))
                    self.offset_lbl.config(width=-(-need // _measure(self.offset_lbl, f, "0")))
                except Exception:
                    self.offset_lbl.config(width=8)     # a stand-in Tk (tests) has no fonts
                self.offset_lbl.pack(side="left", padx=(px(6), 0))
                continue
            label = f"{delta:+.1f}".replace("-", "−")
            widgets.FlatButton(fine, label, kind="chip", height=30, font=font(13),
                               command=lambda d=delta: self._nudge(d)).pack(side="left",
                                                                            padx=(px(6), 0))
        widgets.FlatButton(fine, "Reset", kind="link", height=30, font=font(13), padx=8,
                           command=lambda: self._reset_offset()).pack(side="right")

    # --- helpers for the views ------------------------------------------------

    def _film_tile_clicked(self):
        """The header's film tile shows the film window, as the board's
        link does; the VLC app has its own window."""
        if self.player is self.embedded:
            self._show_video_window()

    def _fit_film_name(self, width):
        """Cut the film's name to the width its label was given."""
        text = self._film_name
        if isinstance(width, int) and width > 1 and text:
            text = widgets.ellipsize(text, theme.font(16, True), width)
        self.file_lbl.config(text=text)

    def _fit_controls(self):
        """Cut the subtitle track's name so the controls row fits the card.

        A track name can be long ("English (SDH) - Forced"), and every
        button asks for the width of its text; uncut, the row would widen
        the window. The mute button's width changes with its text, so this
        runs after either changes."""
        text = f"Subtitles: {self._subs_text}"
        self.subs_btn.config(text=text)
        # A button's requested width is current as soon as it is configured
        # (a Frame's would wait for the next idle pass).
        widths = [w.winfo_reqwidth() for w in (self.play_btn, self._full_btn, self.mute_btn,
                                               self.subs_btn)]
        if not all(isinstance(w, int) for w in widths):
            return               # a stand-in Tk (tests) has no geometry
        over = sum(widths) + 3 * theme.px(CONTROLS_GAP) - theme.px(CARD_INNER_W)
        if over > 0:
            f = theme.font(14)
            full = _measure(self.subs_btn, f, text)
            self.subs_btn.config(text=widgets.ellipsize(text, f, max(1, full - over)))

    def _cap_status_lines(self):
        """At most two lines of status: a long engine message is cut, not
        allowed to push the cards apart. The text itself stays whole, for
        status_lbl.cget("text")."""
        lbl = self.status_lbl
        lbl.config(height=0)
        need = lbl.winfo_reqheight()
        if not isinstance(need, int):
            return               # a stand-in Tk (tests) has no geometry
        line = int(lbl.tk.call("font", "metrics", lbl.cget("font"), "-displayof", lbl,
                               "-linespace"))
        if need > 2 * line:
            lbl.config(height=2)

    def _place_status(self):
        """Pack the status line into the board on show, or hide it."""
        px, lbl = theme.px, self.status_lbl
        lbl.pack_forget()
        if not self._status_shown:
            return
        if self._in_main:
            lbl.config(bg=theme.CARD, wraplength=px(CARD_INNER_W))
            lbl.pack(after=self._state_row, fill="x", pady=(px(6), 0))
        else:
            lbl.config(bg=theme.GROUND, wraplength=px(CONTENT_W))
            lbl.pack(after=self._listen_row, fill="x", pady=(px(10), 0))
        self._cap_status_lines()

    # --- views --------------------------------------------------------------------

    def _show_main_state(self):
        """FirstRun with no film, Main with one."""
        main = bool(self.ctl.video_path)
        if main != self._in_main:
            self._in_main = main
            if main:
                self.first_frame.pack_forget()
                self.main_frame.pack(fill="x")
            else:
                self.main_frame.pack_forget()
                self.first_frame.pack(fill="both", expand=True)
            self._place_status()

    def _show_film(self, name, length):
        """The header: the film's name, its length when known, and which
        player plays it."""
        self._film_name = name or ""
        self._fit_film_name(self.file_lbl.winfo_width())
        built_in = self.player is self.embedded
        player = "built-in player" if built_in else "VLC app"
        if length:
            self._film_info.config(text=f"{widgets.fmt_clock(length)} · {player}")
        else:
            self._film_info.config(text=player[:1].upper() + player[1:])
        self._film_tile.config(cursor="hand2" if built_in else "")

    def _show_film_link(self, visible):
        if visible:
            self.film_link.pack(side="left", padx=(theme.px(8), 0))
        else:
            self.film_link.pack_forget()

    def _show_sync_state(self, state, at=None):
        """The sync-state row, as SYNC_STATES has it. Returns the
        headline as plain text, for the fullscreen bar's status."""
        color, strong, rest = SYNC_STATES[state]
        if "{at}" in rest:
            rest = rest.format(at=widgets.fmt_clock(at)) if at is not None else ""
        self._sync_dot.set(getattr(theme, color))
        self._sync_strong.config(text=strong)
        self._sync_rest.config(text=rest)
        if state not in AGO_STATES:
            self._ago_lbl.config(text="")
        return f"{strong}{rest}"

    def _show_ago(self, text):
        self._ago_lbl.config(text=text)

    def _show_auto_sub(self):
        try:
            interval = self.interval_var.get()
        except (tk.TclError, ValueError):
            interval = 30        # the dialogs validate it; this is only a label
        text = f"Re-checks every {interval} s"
        if self.follow_var.get():
            text += " and follows the stream's pauses"
        self._auto_sub.config(text=text)

    def _show_position(self, t, n, playing):
        """The timeline, both times and the play button. With no time (no
        film, or stopped) it reads 0:00 with the knob at the start."""
        if self.timeline.dragging is not True:     # the drag's preview owns the label
            self._pos_lbl.config(text=widgets.fmt_clock(t or 0))
            if t is None:
                self.timeline.set(0.0)
            else:
                self.timeline.set(min(1.0, max(0.0, t / n)) if n else None)
        self._len_lbl.config(text=widgets.fmt_clock(n) if n else "-:--")
        icon = "pause" if playing else "play"
        if self.play_btn.cget("icon") != icon:
            self.play_btn.config(icon=icon)

    def _preview_position(self, frac):
        """While the timeline is dragged: the time under the knob. The film
        moves once, on release (Timeline calls _seek_to_frac)."""
        n = self._pos[1] or self._film_length
        if n:
            self._pos_lbl.config(text=widgets.fmt_clock(frac * n))

    def _show_mute(self):
        if self.mute_var.get():
            self.mute_btn.config(text="Film muted", icon="speaker_muted", kind="secondary",
                                 outline=None)
        else:
            # An ACCENT outline while the film's sound is on: a loopback
            # capture hears the film too, which spoils the next sync.
            self.mute_btn.config(text="Film sound on", icon="speaker_on", kind="ghost",
                                 outline=theme.ACCENT)
        self._fit_controls()

    def _show_subtitle(self, text):
        """subs_btn reads "Subtitles: " + text: "Off", a track's name or
        "in VLC"."""
        self._subs_text = text
        self._fit_controls()

    def _show_offset(self, offset):
        self.offset_lbl.config(text=f"{offset:+.2f} s")

    def _show_party_live(self, on):
        if on:
            self._party_dot.pack(side="left", after=self.party_btn, padx=(theme.px(6), 0))
        else:
            self._party_dot.pack_forget()

    def _show_listen(self, label):
        """FirstRun's line saying which sound StreamSync listens to; an
        empty label is the default device."""
        label = label or self.DEFAULT_DEVICE_NOTE or "default device"
        text, tail = f"Listening to: {label}", " · "
        # A device's name can be long ("CABLE Output (VB-Audio Virtual
        # Cable)"): cut it, rather than let the line widen the window.
        link = self._listen_link.winfo_reqwidth()
        if isinstance(link, int):
            f = theme.font(13)
            room = theme.px(CONTENT_W) - link - _measure(self._listen_lbl, f, tail)
            text = widgets.ellipsize(text, f, max(1, room))
        self._listen_lbl.config(text=text + tail)

    def _show_status_line(self, visible):
        """Show or hide status_lbl's line; the Shell sets its text."""
        self._status_shown = bool(visible)
        self._place_status()


# --- the gallery: python -m ui.mainwin [--scale 1.5] ----------------------------

FILM_NAME = "Example Film (2014).mkv"
FILM_LENGTH = 10143.0        # 2:49:03, as on the boards
FILM_AT = 4360.0             # 1:12:40


class _Gallery(MainWindow):
    """A stand-in app for the gallery: the state the Shell keeps, and
    actions that only move the views, so every control can be tried.
    Nothing here loads a film, plays sound or reads a config."""

    DEFAULT_DEVICE_NOTE = "system default"

    def __init__(self, root, film):
        self.root = root
        root.configure(bg=theme.GROUND)
        self._pos = (None, None)
        self._film_length = None
        self.player_var = tk.StringVar(root, value="embedded")
        self.mute_var = tk.BooleanVar(root, value=True)
        self.auto_var = tk.BooleanVar(root, value=False)
        self.follow_var = tk.BooleanVar(root, value=True)
        self.interval_var = tk.IntVar(root, value=30)
        self.device_var = tk.StringVar(root, value="")
        self.embedded = self.player = SimpleNamespace(name="built-in")
        self.ctl = SimpleNamespace(video_path=None)
        self._t, self._playing, self._offset, self._sub = None, False, 0.0, 0
        self._statuses = 0
        self._build_main()
        if film:
            self._choose_file()
            self._t, self._playing = FILM_AT, True
            self.auto_var.set(True)
            self._nudge(0.2)
            self._show_sync_state("synced", FILM_AT)
            self._show_ago("2 s ago")
            self._tick()

    def _tick(self):
        self._pos = (self._t, FILM_LENGTH if self.ctl.video_path else None)
        self._show_position(self._t, self._pos[1], self._playing)

    def _device_label(self, value):
        return value

    def _choose_file(self):
        self.ctl.video_path = FILM_NAME
        self._film_length = FILM_LENGTH
        self._show_film(FILM_NAME, FILM_LENGTH)
        self._show_main_state()
        self._show_sync_state("none")
        self._tick()

    def _sync(self):
        self._show_sync_state("syncing")
        self.root.after(1200, lambda: (self._show_sync_state("synced", self._t or 0),
                                       self._show_ago("0 s ago")))

    def _resync(self):
        self._show_sync_state("missed")

    def _on_auto_toggle(self):
        self._show_auto_sub()

    def _can_seek(self):
        return self._pos[1] is not None

    def _seek_to_frac(self, frac):
        self._t = frac * FILM_LENGTH
        self._show_sync_state("moved")
        self._tick()

    def _toggle_pause(self):
        self._playing = not self._playing
        self._tick()

    def _toggle_fullscreen(self):
        self._show_film_link(True)

    def _show_video_window(self):
        self._show_film_link(False)

    def _toggle_mute(self):
        self.mute_var.set(not self.mute_var.get())
        self._show_mute()

    def _open_subs_menu(self):
        self._sub = (self._sub + 1) % 3
        self._show_subtitle(("Off", "English", "English (SDH) - Forced, signs and songs only")
                            [self._sub])

    def _nudge(self, delta):
        self._offset += delta
        self._show_offset(self._offset)

    def _reset_offset(self):
        self._offset = 0.0
        self._show_offset(0.0)

    def _open_settings(self):
        """Cycles the status line: none, one line, a long one cut to two."""
        self._statuses = (self._statuses + 1) % 3
        self.status_lbl.config(text=("", "Matched at 1:12:40 (score 0.83).",
                                     "Couldn't list the audio devices: " + "the device "
                                     "went away while it was being opened, " * 6)
                               [self._statuses])
        self._show_status_line(bool(self.status_lbl.cget("text")))

    def _open_watch_party(self):
        self._live = not getattr(self, "_live", False)
        self._show_party_live(self._live)


def gallery(scale=1.0):
    """Both boards over the stand-in app at one scale, each in a Toplevel:
    FirstRun, and Main (synced and playing, as on the board).
    Returns (root, first_run_app, main_app); the caller runs the loop.
    One scale per process: theme's scale is global."""
    root = tk.Tk()
    root.withdraw()              # each board gets a window of its own
    theme.init(scale, ".AppleSystemUIFont" if sys.platform == "darwin" else "Segoe UI")
    pct = f"{round(scale * 100)}%"
    apps = []
    for n, (name, film) in enumerate((("first run", False), ("main", True))):
        top = tk.Toplevel(root)
        top.title(f"StreamSync - gallery, {name}, {pct}")
        top.resizable(False, False)
        top.geometry(f"+{40 + n * (theme.px(560) + 40)}+40")
        top.protocol("WM_DELETE_WINDOW", root.destroy)
        apps.append(_Gallery(top, film=film))
    return root, apps[0], apps[1]


def _gallery_main(argv):
    import subprocess
    from pathlib import Path
    scale = float(argv[argv.index("--scale") + 1]) if "--scale" in argv else None
    if scale is None:
        # 150% in a process of its own, since the scale is global
        here = Path(__file__).resolve().parent.parent
        subprocess.Popen([sys.executable, "-m", "ui.mainwin", "--scale", "1.5"], cwd=here)
        scale = 1.0
    root, _, _ = gallery(scale)
    root.mainloop()


if __name__ == "__main__":
    _gallery_main(sys.argv[1:])
