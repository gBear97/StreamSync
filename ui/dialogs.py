"""The Settings, Watch party and Update dialogs.

Each dialog is a transient Toplevel whose title starts with "StreamSync"
(Windows lists the open windows as stream candidates and leaves out ours
only by that prefix). Settings and Update take the grab; Watch party does
not, and opens beside the main window when the screen has room: a host
plays, pauses and moves the film from the main window with it open. Each
registers itself on the app - app._settings, app._party or
app._update_dialog - *before* app._present() runs: on the Mac that pumps
events, and a second click on the button that opened it must find the
dialog already open. close() releases any grab, destroys the window and
unregisters it, and gives the grab back to the dialog that held it before
(Settings, under an Update dialog that opened over it).

The dialogs only show and collect; every action goes through the app's
methods (ui/contract.py), so the same code runs on both platforms. Their
texts come from the platform's constants where they differ.

`python -m ui.dialogs` opens each of them over a stub app - a gallery for
checking the screens against the boards, at any scale.
"""

import math
import tkinter as tk
from pathlib import Path
from tkinter import messagebox
from urllib.parse import urlsplit

import controller
import hosting
import session
from ui import theme, widgets
from version import __version__

DEFAULT_MIC = "Default microphone"     # the mic pick that means mic_name=None
SCROLL_STEP = 20                       # design px: Settings' scroll unit, wheel and focus alike


def _bg(parent):
    try:
        return parent.cget("bg")
    except tk.TclError:
        return theme.GROUND


def _label(parent, text="", size=14, bold=False, fg=None, wrap=None, **kw):
    """A plain label on its parent's background, in the theme's font. Its
    text sits at the left, also where the label is stretched across its
    row (a tk.Label centres it)."""
    kw.setdefault("anchor", "w")
    return tk.Label(parent, text=text, font=theme.font(size, bold), fg=fg or theme.TEXT,
                    bg=_bg(parent), bd=0, padx=0, pady=0, justify="left",
                    wraplength=theme.px(wrap) if wrap else 0, **kw)


def _frame(parent, **kw):
    return tk.Frame(parent, bg=_bg(parent), bd=0, highlightthickness=0, **kw)


def _h2(parent, text):
    """A Settings section heading: small, bold, muted capitals."""
    return _label(parent, text.upper(), 12, True, theme.MUTED)


def _destroy_later(root, widget):
    """Destroy `widget` once the current callback has returned, through
    the root. A drawn button runs its command from an after() callback
    of its own; destroying the button inside that command (Done, Later,
    Close) would leave tkinter's after() cleaning up a command the
    destroyed widget no longer holds, which raises into the Tk hook."""
    def destroy():
        try:
            widget.destroy()
        except tk.TclError:
            pass                         # gone with its window already
    try:
        root.after_idle(destroy)
    except tk.TclError:                  # the app is closing: the root takes it along
        pass


def _show(widget, on, **pack):
    """Pack `widget` where `pack` says, or take it out of the layout."""
    if on:
        widget.pack(**pack)
    else:
        widget.pack_forget()


def _within(widget, ancestor):
    """`widget` (or its path) is `ancestor` or one of its descendants."""
    path, top = str(widget), str(ancestor)
    return path == top or path.startswith(top + ".")


class _Dialog:
    """What the three share: the window, its registration on the app as
    `slot`, its grab (if `modal`), and close()."""

    slot = ""
    width = 600                          # design px
    modal = True                         # holds the grab while open

    def _open(self, app, title):
        self.app = app
        self._closed = False
        self._prev_grab = None
        self.win = win = tk.Toplevel(app.root)
        win.withdraw()                   # built out of sight, shown by _show_built
        win.title(title)
        win.configure(bg=theme.GROUND)
        win.resizable(False, False)
        win.transient(app.root)
        return win

    def _show_built(self):
        """Register, place, style and present the built window, then grab."""
        win, app = self.win, self.app
        setattr(app, self.slot, self)
        self._place()
        win.deiconify()
        app._style_window(win)
        app._present(win)
        self._grab()
        try:
            win.focus_force()            # Esc and Return reach it at once
        except tk.TclError:
            pass

    def _place(self):
        # Over the main window rather than wherever the window manager
        # puts a new one (the screen's corner on Windows). Cosmetic: any
        # failure leaves the default.
        win, root = self.win, self.app.root
        try:
            win.update_idletasks()
            w = win.winfo_reqwidth()
            x = root.winfo_rootx() + (root.winfo_width() - w) // 2
            y = root.winfo_rooty() + theme.px(40)
            x = max(0, min(x, win.winfo_screenwidth() - w))
            y = max(0, min(y, win.winfo_screenheight() - win.winfo_reqheight() - theme.px(40)))
            win.geometry(f"+{x}+{y}")
        except (tk.TclError, TypeError, ValueError):
            pass

    def _grab(self, tries=10):
        # A window the window manager has not mapped yet cannot take the
        # grab on every platform; it is asked again shortly.
        if self._closed or not self.modal:
            return
        if self._prev_grab is None:
            try:
                current = self.win.grab_current()
            except Exception:            # a grab window tkinter cannot name
                current = None
            if current is not None and str(current) != str(self.win):
                self._prev_grab = current
        try:
            self.win.grab_set()
        except tk.TclError:
            if tries > 0:
                # through the root: the dialog may be gone by then
                self.app.root.after(50, self._grab, tries - 1)

    def lift(self):
        """Bring the open dialog back to the front, with the grab."""
        try:
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()
        except tk.TclError:
            return
        self._grab()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if getattr(self.app, self.slot, None) is self:
            setattr(self.app, self.slot, None)
        try:
            self.win.grab_release()
            self.win.withdraw()
        except tk.TclError:
            pass                         # already gone
        _destroy_later(self.app.root, self.win)
        prev = self._prev_grab
        if prev is not None:
            try:
                if prev.winfo_exists():
                    prev.grab_set()
            except tk.TclError:
                pass


# --- Settings ----------------------------------------------------------------


class SettingsDialog(_Dialog):
    """Listening, player, syncing, updates and the keys.

    The two text fields edit copies of the shared values. A value reaches
    hint_var or interval_var only once it is valid, so the engine never
    reads a half-typed hint and IntVar.get() never meets "abc"."""

    slot = "_settings"
    width = 600
    PAD_X, PAD_Y, GAP = 28, 22, 22       # design px

    def __init__(self, app):
        win = self._open(app, "StreamSync - Settings")
        px = theme.px
        self.hint_text = tk.StringVar(win, value=app.hint_var.get())
        try:
            interval = str(app.interval_var.get())
        except (tk.TclError, ValueError):
            interval = str(app.ctl.auto_interval)
        self.interval_text = tk.StringVar(win, value=interval)

        # The footer stays put; everything above it scrolls when the
        # screen is too short for it (the board is 860 tall).
        footer = _frame(win)
        footer.pack(side="bottom", fill="x", padx=px(self.PAD_X),
                    pady=(px(self.GAP), px(self.PAD_Y)))
        self.done_btn = widgets.FlatButton(footer, "Done", kind="primary", height=38,
                                           font=theme.font(14, True), padx=22,
                                           command=self.done)
        self.done_btn.pack(side="right")
        self._footer = footer

        area = _frame(win)
        area.pack(side="top", fill="both", expand=True)
        self._canvas = canvas = tk.Canvas(area, bg=theme.GROUND, highlightthickness=0, bd=0,
                                          width=px(self.width), height=px(200),
                                          yscrollincrement=px(SCROLL_STEP))
        # drawn, as the other controls are: a native scrollbar would be a
        # light strip down the dark dialog
        self._scrollbar = widgets.Scrollbar(area, command=canvas.yview)
        canvas.configure(yscrollcommand=self._scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        inner = _frame(canvas)
        self._item = canvas.create_window(0, 0, window=inner, anchor="nw", width=px(self.width))
        self._inner = inner
        body = _frame(inner)
        body.pack(fill="x", padx=px(self.PAD_X), pady=(px(self.PAD_Y), 0))

        inner_w = self.width - 2 * self.PAD_X
        self._section(body, "Listening", 8, self._build_listening, inner_w)
        self._section(body, "Player", 10, self._build_player, inner_w)
        self._section(body, "Syncing", 12, self._build_syncing, inner_w)
        self._section(body, "Updates", 10, self._build_updates, inner_w)
        self._section(body, "Keys", 10, self._build_keys, inner_w, last=True)
        # Made first so that pack keeps it at the bottom, the footer would
        # be first in the Tab order too: Done comes last, where it sits.
        widgets.tab_last(footer)

        inner.bind("<Configure>", lambda e: self._fit_scroll())
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            win.bind(seq, self._on_wheel, add="+")
        # every control in the dialog carries the window's bindtag
        self._focus_seen = None
        win.bind("<FocusIn>", self._follow_focus, add="+")
        win.protocol("WM_DELETE_WINDOW", self.done)
        win.bind("<Escape>", lambda e: self.done())
        self._fit_scroll()

        self._show_built()
        app._populate_audio_devices()
        if app.REFRESH_STREAMS_ON_SETTINGS:
            app._refresh_stream_list()

    # --- building --------------------------------------------------------

    def _section(self, parent, title, gap, build, inner_w, last=False):
        sec = _frame(parent)
        sec.pack(fill="x", pady=(0, 0 if last else theme.px(self.GAP)))
        _h2(sec, title).pack(anchor="w")
        build(sec, gap, inner_w)

    def _build_listening(self, sec, gap, inner_w):
        app, px = self.app, theme.px
        _label(sec, "Listen to", 14, True).pack(anchor="w", pady=(px(gap), 0))
        self.device_select = widgets.Select(sec, app.device_var, values=app._device_items,
                                            label_for=app._device_label,
                                            command=app._on_device_pick, height=36)
        self.device_select.pack(fill="x", pady=(px(gap), 0))
        _label(sec, app.LISTEN_HELP, 12, fg=theme.MUTED, wrap=inner_w).pack(
            anchor="w", pady=(px(gap), 0))

    def _build_player(self, sec, gap, inner_w):
        app, px = self.app, theme.px
        for text, value, sub in (
                ("Built-in player", "embedded",
                 "The most precise sync, with fullscreen controls and subtitles."),
                ("VLC app", "external", "Plays in your installed VLC's own window instead.")):
            widgets.Radio(sec, text, app.player_var, value, sub=sub,
                          command=app._apply_player_choice).pack(anchor="w", pady=(px(gap), 0))

    def _build_syncing(self, sec, gap, inner_w):
        app, px = self.app, theme.px
        grid = _frame(sec)
        grid.pack(fill="x", pady=(px(gap), 0))
        col_w = (inner_w - 14) // 2
        for c in (0, 1):
            grid.columnconfigure(c, weight=1, uniform="syncing", minsize=px(col_w))

        left = _frame(grid)
        left.grid(row=0, column=0, sticky="nwe", padx=(0, px(7)))
        _label(left, "Start searching near", 14, True).pack(anchor="w")
        self.hint_field = widgets.Field(left, textvariable=self.hint_text, height=36,
                                        placeholder="h:mm:ss")
        self.hint_field.pack(fill="x", pady=(px(6), 0))
        self.hint_err = _label(left, "", 12, fg=theme.ERROR, wrap=col_w)
        _label(left, "Optional. Blank searches the whole film.", 12, fg=theme.MUTED,
               wrap=col_w).pack(anchor="w", pady=(px(6), 0))

        right = _frame(grid)
        right.grid(row=0, column=1, sticky="nwe", padx=(px(7), 0))
        _label(right, "Re-check every", 14, True).pack(anchor="w")
        row = _frame(right)
        row.pack(anchor="w", pady=(px(6), 0))
        self.interval_field = widgets.Field(row, textvariable=self.interval_text, width=72,
                                            height=36)
        self.interval_field.pack(side="left")
        _label(row, "seconds").pack(side="left", padx=(px(8), 0))
        self.interval_err = _label(right, "", 12, fg=theme.ERROR, wrap=col_w)
        _label(right, 'While "Keep in sync automatically" is on.', 12, fg=theme.MUTED,
               wrap=col_w).pack(anchor="w", pady=(px(6), 0))

        for field, apply in ((self.hint_field, self._apply_hint),
                             (self.interval_field, self._apply_interval)):
            field.entry.bind("<Return>", lambda e, f=apply: f(True), add="+")
            field.entry.bind("<KP_Enter>", lambda e, f=apply: f(True), add="+")
            field.entry.bind("<FocusOut>", lambda e, f=apply: f(False), add="+")

        widgets.Check(sec, "Follow the stream's pauses", app.follow_var,
                      command=app._on_auto_toggle).pack(anchor="w", pady=(px(gap), 0))
        widgets.Check(sec, "Show the stream while the film is paused", app.swap_var,
                      command=self._swap_changed).pack(anchor="w", pady=(px(gap), 0))
        row = _frame(sec)
        row.pack(fill="x", padx=(px(26), 0), pady=(px(gap), 0))
        self.stream_lbl = _label(row, app.STREAM_LABEL, fg=theme.MUTED)
        self.stream_lbl.pack(side="left")
        self.refresh_btn = widgets.FlatButton(row, "Refresh", height=34,
                                              command=app._refresh_stream_list)
        self.refresh_btn.pack(side="right")
        self.stream_select = widgets.Select(
            row, app.stream_var, values=[""] + list(app._stream_choices),
            label_for=lambda v: v or app.STREAM_AUTO_LABEL, command=app._on_stream_pick,
            height=34, width=200)
        self.stream_select.pack(side="left", fill="x", expand=True, padx=px(10))
        # made before the list so that pack keeps it at the row's end, and
        # reached after it, where it sits
        widgets.tab_last(self.refresh_btn)
        self._sync_stream_row()

    def _build_updates(self, sec, gap, inner_w):
        app, px = self.app, theme.px
        widgets.Check(sec, "Check for updates when StreamSync starts", app.check_updates_var,
                      command=app._save_config).pack(anchor="w", pady=(px(gap), 0))
        row = _frame(sec)
        row.pack(fill="x", pady=(px(gap), 0))
        self.check_btn = widgets.FlatButton(row, "Check now", height=34)
        self.check_btn.pack(side="right", padx=(px(12), 0))
        self.update_lbl = _label(row, "", fg=theme.MUTED, wrap=inner_w - 130)
        self.update_lbl.pack(side="left", fill="x")
        action = (None if app._update_checking
                  else "update" if app._update_offer else "check")
        self.set_update_line(app._update_line, action)

    def _build_keys(self, sec, gap, inner_w):
        px = theme.px
        grid = _frame(sec)
        grid.pack(fill="x", pady=(px(gap), 0))
        for c in (0, 1):
            grid.columnconfigure(c, weight=1, uniform="keys")
        for i, (key, what) in enumerate((("Space", "Play / pause"),
                                         ("← →", "Back / forward 5 s"),
                                         ("F", "Fullscreen"), ("Esc", "Leave fullscreen"))):
            cell = _frame(grid)
            cell.grid(row=i // 2, column=i % 2, sticky="w",
                      padx=(0, px(7)) if i % 2 == 0 else (px(7), 0),
                      pady=(0, px(8)) if i < 2 else 0)
            cell.columnconfigure(0, minsize=px(54))
            widgets.Kbd(cell, key).grid(row=0, column=0, sticky="ew")
            _label(cell, what, 13).grid(row=0, column=1, sticky="w", padx=(px(10), 0))

    # --- scrolling ---------------------------------------------------------

    def _fit_scroll(self):
        # The canvas asks for the content's height, clamped to what the
        # screen has room for. The toplevel's requested size is therefore
        # already right, and the Mac's present_window nudge, which resizes
        # the window to its requested size, cannot undo it.
        if self._closed:
            return
        try:
            self.win.update_idletasks()
            h = self._inner.winfo_reqheight()
            room = (self.win.winfo_screenheight() - theme.px(80)
                    - self._footer.winfo_reqheight() - theme.px(self.GAP + self.PAD_Y))
        except tk.TclError:
            return
        shown = max(theme.px(120), min(h, room))
        self._canvas.configure(height=shown, scrollregion=(0, 0, theme.px(self.width), h))
        self._scrolls = h > shown
        _show(self._scrollbar, self._scrolls, side="right", fill="y", before=self._canvas)
        # The scrollbar takes its width out of the right-hand padding, so
        # the window stays the board's width either way.
        bar = self._scrollbar.winfo_reqwidth() if self._scrolls else 0
        self._canvas.configure(width=theme.px(self.width) - bar)

    def _follow_focus(self, event):
        # Tab, or a field taking the keyboard back to show its error, can
        # land on a control the scroll area has out of sight: the view
        # moves, in whole scroll steps, until the control and a section
        # gap around it show. The keyboard coming back to the control that
        # last had it is the window being activated again, so the view
        # stays where the wheel left it.
        w = event.widget
        try:
            # Tk tells the frames on the way to a focused control too, and
            # the window itself when it is activated: only the control counts
            if self._closed or w is self.win or str(w) != str(self.win.focus_get()):
                return
        except (tk.TclError, KeyError):
            return
        seen, self._focus_seen = self._focus_seen, str(w)
        if seen == str(w) or not getattr(self, "_scrolls", False) or not _within(w, self._inner):
            return
        # a field's box is its frame, around the entry that takes the keys
        box = w.master if isinstance(w.master, widgets.Field) else w
        canvas = self._canvas
        try:
            top = box.winfo_rooty() - self._inner.winfo_rooty()
            bottom = top + box.winfo_height()
            view, height = canvas.canvasy(0), canvas.winfo_height()
        except tk.TclError:
            return
        margin, step = theme.px(self.GAP), theme.px(SCROLL_STEP)
        if top - margin < view:
            canvas.yview_scroll(-math.ceil((view - top + margin) / step), "units")
        elif bottom + margin > view + height:
            canvas.yview_scroll(math.ceil((bottom + margin - view - height) / step), "units")

    def _on_wheel(self, event):
        if not getattr(self, "_scrolls", False):
            return
        try:
            over = self.win.winfo_containing(event.x_root, event.y_root)
        except (tk.TclError, KeyError):
            return
        if over is None or not (_within(over, self._canvas) or _within(over, self._scrollbar)):
            return
        if event.num == 4:
            steps = -1
        elif event.num == 5:
            steps = 1
        elif abs(event.delta) >= 120:    # a Windows notch
            steps = -int(event.delta / 120) * 2
        else:                            # the Mac's small deltas
            steps = -1 if event.delta > 0 else 1 if event.delta < 0 else 0
        if steps:
            self._canvas.yview_scroll(steps, "units")

    # --- fields ----------------------------------------------------------------

    def _check_hint(self, show):
        text = self.hint_text.get().strip()
        try:
            controller.parse_time(text)
            ok = True
        except ValueError:
            ok = False
        if show or ok:
            self._set_error(self.hint_field, self.hint_err,
                            "" if ok else "Use h:mm:ss, m:ss or seconds.")
        return text if ok else None

    def _check_interval(self, show):
        text = self.interval_text.get().strip()
        try:
            n = int(text)
        except ValueError:
            n = None
        ok = n is not None and 10 <= n <= 300
        if show or ok:
            self._set_error(self.interval_field, self.interval_err,
                            "" if ok else "A whole number from 10 to 300.")
        return n if ok else None

    def _set_error(self, field, lbl, text):
        if self._closed:
            return
        field.set_error(bool(text))
        lbl.config(text=text)
        # under the field, or under the row that holds it with its unit
        under = field if field.master is lbl.master else field.master
        _show(lbl, bool(text), anchor="w", pady=(theme.px(4), 0), after=under)

    def _apply_hint(self, show=False):
        if self._closed:
            return False
        hint = self._check_hint(show)
        if hint is None:
            return False
        if hint != self.app.hint_var.get():
            self.app.hint_var.set(hint)
            self.app._save_config()
        return True

    def _apply_interval(self, show=False):
        if self._closed:
            return False
        n = self._check_interval(show)
        if n is None:
            return False
        try:
            same = self.app.interval_var.get() == n
        except (tk.TclError, ValueError):
            same = False
        if not same:
            self.app.interval_var.set(n)
            self.app._on_auto_toggle()   # hands the interval to the engine, and saves
        return True

    def apply(self):
        """Validate both fields and apply them. On an error the message
        shows, the field keeps the focus and this returns False."""
        hint_ok = self._check_hint(True) is not None
        interval_ok = self._check_interval(True) is not None
        if not (hint_ok and interval_ok):
            (self.hint_field if not hint_ok else self.interval_field).entry.focus_set()
            return False
        self._apply_hint()
        self._apply_interval()
        self.app._save_config()
        return True

    def done(self):
        if self.apply():
            self.close()

    # --- the stream row and the app's updates ------------------------------

    def _swap_changed(self):
        self.app._save_config()
        self._sync_stream_row()

    def _sync_stream_row(self):
        # the pick only matters while the stream is shown during pauses
        on = bool(self.app.swap_var.get())
        state = "normal" if on else "disabled"
        self.stream_lbl.config(fg=theme.MUTED if on else theme.DISABLED_FG)
        self.stream_select.config(state=state)
        self.refresh_btn.config(state=state)

    def set_devices(self, items):
        self.device_select.set_values(items)

    def set_stream_choices(self, items):
        self.stream_select.set_values([""] + list(items))

    def set_update_line(self, text, action):
        app = self.app
        self.update_lbl.config(text=text)
        if action == "update":
            self.check_btn.config(text="Update…", state="normal",
                                  command=app._show_update_offer)
        else:
            self.check_btn.config(text="Check now",
                                  state="disabled" if action is None else "normal",
                                  command=lambda: app._check_updates(quiet=False))


# --- Watch party -------------------------------------------------------------


def relay_address(text):
    """The relay address the Relay server field holds, or None if it holds
    none. An https:// or http:// address - what cloudflared prints for a
    tunnel - stands for the wss:// or ws:// one, which is what is meant:
    websockets refuses it as typed."""
    text = text.strip()
    try:
        scheme = urlsplit(text).scheme
        if scheme in ("https", "http"):
            text = {"https": "wss", "http": "ws"}[scheme] + text[len(scheme):]
        parts = urlsplit(text)
        parts.port                    # a port that is no port raises
    except ValueError:
        return None
    return text if parts.scheme in ("ws", "wss") and parts.hostname else None


class WatchPartyDialog(_Dialog):
    """Host and Join side by side, the relay underneath; while a session
    runs, its code, its address with Copy invite, its latest line and
    Leave instead.

    Not modal: a host plays and pauses the film in the main window while
    this shows the code. With the grab it held before, the main window
    ignored every click until this was closed - and nothing said so.

    Hosting goes through a free Cloudflare tunnel from this computer unless
    that is unticked (then: the relay server below). The tunnel opens on a
    worker thread (app._host_here) and the Host button reads Cancel
    meanwhile; a missing cloudflared is downloaded only once the user
    says yes, and otherwise the way to get it is shown."""

    slot = "_party"
    width = 680
    modal = False
    PAD_X, PAD_Y = 26, 22
    GAP = 12                             # design px between it and the main window

    def _place(self):
        # Beside the main window, right then left, when the screen has
        # room, so the film's controls stay in sight; over it, as the other
        # dialogs open, when it has not.
        win, root = self.win, self.app.root
        try:
            win.update_idletasks()
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            gap = theme.px(self.GAP)
            rx, ry, rw = root.winfo_rootx(), root.winfo_rooty(), root.winfo_width()
            if rx + rw + gap + w <= win.winfo_screenwidth():
                x = rx + rw + gap
            elif rx - gap - w >= 0:
                x = rx - gap - w
            else:
                super()._place()
                return
            y = max(0, min(ry, win.winfo_screenheight() - h - theme.px(40)))
            win.geometry(f"+{x}+{y}")
        except (tk.TclError, TypeError, ValueError):
            super()._place()

    def __init__(self, app):
        win = self._open(app, "StreamSync - Watch party")
        px = theme.px
        self._mics = []
        self._shown_code = None
        self.src_var = tk.StringVar(win, value="listen")
        self.mic_var = tk.StringVar(win, value=DEFAULT_MIC)
        self.relay_var = tk.StringVar(win, value=app.ctl.relay_url)
        self.tunnel_var = tk.BooleanVar(win, value=app.ctl.host_via != "relay")
        self._shown_public = None        # a tunnel's address the relay field shows
        self._progress = False           # the Host note is the tunnel's progress

        outer = _frame(win)
        outer.pack(fill="both", expand=True, padx=px(self.PAD_X), pady=px(self.PAD_Y))
        # the board's width, whichever view shows
        _frame(outer, width=px(self.width - 2 * self.PAD_X), height=0).pack()
        self._views = _frame(outer)
        self._views.pack(fill="both", expand=True)
        col_w = (self.width - 2 * self.PAD_X - 16) // 2
        self._build_idle(self._views, col_w)
        self._build_running(self._views)

        # the relay row
        _frame(outer, height=px(18)).pack(fill="x")
        widgets.Divider(outer).pack(fill="x")
        row = _frame(outer)
        row.pack(fill="x", pady=(px(14), 0))
        _label(row, "Relay server", 13, fg=theme.MUTED).pack(side="left")
        self.close_btn = widgets.FlatButton(row, "Close", kind="ghost", height=34, padx=16,
                                            command=self.close)
        self.close_btn.pack(side="right")
        self.relay_field = widgets.Field(row, textvariable=self.relay_var, height=32,
                                         font=theme.font(13))
        self.relay_field.pack(side="left", fill="x", expand=True, padx=px(10))
        self.relay_field.entry.configure(readonlybackground=theme.FIELD)
        # made before the field so that pack keeps it at the row's end, and
        # reached after it, where it sits
        widgets.tab_last(self.close_btn)

        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Escape>", lambda e: self.close())
        self.refresh()
        self._show_built()
        app._populate_mics()

    def _build_idle(self, parent, col_w):
        px = theme.px
        self.idle = grid = _frame(parent)
        for c in (0, 1):
            grid.columnconfigure(c, weight=1, uniform="party", minsize=px(col_w))
        grid.rowconfigure(0, weight=1)
        wrap = col_w - 2 * 18 - 2

        host = widgets.Card(grid, padding=18)
        host.grid(row=0, column=0, sticky="nsew", padx=(0, px(8)))
        b = host.body
        _label(b, "Host", 17, True).pack(anchor="w")
        self.host_text = _label(b, "", 13, fg=theme.MUTED, wrap=wrap)
        self.host_text.pack(anchor="w", pady=(px(12), 0))
        lbl = _frame(b)
        lbl.pack(anchor="w", pady=(px(12), 0))
        _label(lbl, "Password", 14, True).pack(side="left")
        _label(lbl, " (optional)", 14, fg=theme.MUTED).pack(side="left")
        self.host_pw = widgets.Field(b, height=36, show="•")
        self.host_pw.pack(fill="x", pady=(px(6), 0))
        _label(b, "Film position from", 14, True).pack(anchor="w", pady=(px(12), 0))
        for text, value in (("What this computer hears", "listen"),
                            ("This app's player", "player")):
            widgets.Radio(b, text, self.src_var, value).pack(anchor="w", pady=(px(6), 0))
        _label(b, "Your microphone", 14, True).pack(anchor="w", pady=(px(12), 0))
        self.mic_select = widgets.Select(b, self.mic_var, values=[DEFAULT_MIC], height=36,
                                         width=200)   # it fills the card's width
        self.mic_select.pack(fill="x", pady=(px(6), 0))
        row = _frame(b)
        row.pack(anchor="w", pady=(px(12), 0))
        _label(row, "Delay hint for viewers", 14, True).pack(side="left")
        # 10, not the board's 0.0: the engine's own default, which stands
        # until a viewer measures its stream's delay. 0 would have viewers
        # play the host's live position, seconds ahead of what they hear.
        self.delay_field = widgets.Field(row, width=64, height=34)
        self.delay_field.set("10")
        self.delay_field.pack(side="left", padx=(px(8), 0))
        _label(row, "s", fg=theme.MUTED).pack(side="left", padx=(px(8), 0))
        # ticked: this computer's relay through a quick tunnel; unticked:
        # the relay server field below the cards
        self.tunnel_check = widgets.Check(b, "Free Cloudflare tunnel", self.tunnel_var,
                                          sub="StreamSync runs the relay for you")
        self.tunnel_check.pack(anchor="w", pady=(px(12), 0))
        self.host_btn = widgets.FlatButton(b, "Start hosting", kind="primary", height=42,
                                           font=theme.font(14, True), command=self._host)
        self.host_btn.pack(side="bottom", fill="x", pady=(px(12), 0))
        self.host_err = _label(b, "", 12, fg=theme.ERROR, wrap=wrap)
        # the tunnel's progress, or how to get cloudflared by hand
        self.host_note = _label(b, "", 12, fg=theme.MUTED, wrap=wrap)
        self.host_link = widgets.FlatButton(b, "Cloudflare's download page", kind="link",
                                            height=22, font=theme.font(12), padx=0,
                                            underline=True,
                                            command=self.app._open_cloudflared_page)

        join = widgets.Card(grid, padding=18)
        join.grid(row=0, column=1, sticky="nsew", padx=(px(8), 0))
        b = join.body
        _label(b, "Join", 17, True).pack(anchor="w")
        _label(b, "Follow a host with your own copy of the film. Open the film first.", 13,
               fg=theme.MUTED, wrap=wrap).pack(anchor="w", pady=(px(12), 0))
        _label(b, "Session code", 14, True).pack(anchor="w", pady=(px(12), 0))
        self.join_code = widgets.Field(b, height=36)
        self.join_code.pack(fill="x", pady=(px(6), 0))
        self.join_code.entry.bind("<KeyRelease>", self._upper_code, add="+")
        _label(b, "Password", 14, True).pack(anchor="w", pady=(px(12), 0))
        self.join_pw = widgets.Field(b, height=36, show="•")
        self.join_pw.pack(fill="x", pady=(px(6), 0))
        self.join_btn = widgets.FlatButton(b, "Join", height=42, font=theme.font(14, True),
                                           command=self._join)
        self.join_btn.pack(side="bottom", fill="x", pady=(px(12), 0))
        self.join_err = _label(b, "", 12, fg=theme.ERROR, wrap=wrap)

    def _build_running(self, parent):
        px = theme.px
        self.running = card = widgets.Card(parent, padding=18)
        b = card.body
        self.live_title = _label(b, "", 17, True)
        self.live_title.pack(anchor="w")
        # an Entry, so the code can be selected and copied
        self.code_field = tk.Entry(b, font=theme.font(22, True), fg=theme.TEXT, bg=theme.CARD,
                                   readonlybackground=theme.CARD, relief="flat", bd=0,
                                   highlightthickness=0, insertbackground=theme.TEXT,
                                   width=14, takefocus=0)
        self.code_field.pack(anchor="w", pady=(px(12), 0))
        self.code_field.configure(state="readonly")
        # where viewers come in, and the line to send them
        self.addr_row = row = _frame(b)
        row.pack(fill="x", pady=(px(10), 0))
        self.invite_btn = widgets.FlatButton(row, "Copy invite", kind="ghost", height=32,
                                             padx=14, command=self._copy_invite)
        self.invite_btn.pack(side="right")
        self.addr_field = tk.Entry(row, font=theme.font(12), fg=theme.MUTED, bg=theme.CARD,
                                   readonlybackground=theme.CARD, relief="flat", bd=0,
                                   highlightthickness=0, insertbackground=theme.TEXT,
                                   takefocus=0)
        self.addr_field.pack(side="left", fill="x", expand=True, padx=(0, px(10)))
        self.addr_field.configure(state="readonly")
        self.addr_note = _label(b, f"A free Cloudflare tunnel from this computer: up to "
                                   f"{hosting.VIEWERS} viewers, for as long as you host.",
                                12, fg=theme.MUTED)
        self.session_lbl = _label(b, "", 13, wrap=self.width - 2 * self.PAD_X - 2 * 18 - 2)
        self.session_lbl.pack(anchor="w", fill="x", pady=(px(12), 0))
        self.leave_btn = widgets.FlatButton(b, "Leave session", height=38,
                                            command=self.app._leave_session)
        self.leave_btn.pack(anchor="w", pady=(px(14), 0))

    # --- views -----------------------------------------------------------

    def refresh(self):
        """The running view while a session runs, else the idle view."""
        if self._closed:
            return
        app = self.app
        running = app._session_running()
        _show(self.idle, not running, fill="both", expand=True)
        _show(self.running, running, fill="both", expand=True)
        self.relay_field.entry.configure(state="readonly" if running else "normal")
        if running:
            viewer = isinstance(app.ctl.session, session.ViewerSession)
            self.live_title.config(text="Watching with a host" if viewer else "Hosting")
            self.session_lbl.config(text=app._session_text)
            self.refresh_code()
            self._show_address()
            if self._progress:
                self._host_feedback()    # the tunnel opened: its progress is done
            return
        self._shown_code = None
        if self._shown_public is not None:
            # the tunnel's address went with its party: back to the saved relay
            if self.relay_var.get() == self._shown_public:
                self.relay_var.set(app.ctl.relay_url)
            self._shown_public = None
        film = bool(app.ctl.video_path)
        self.host_text.config(text="Viewers sync their own copies to you. Only your voice is "
                                   "sent." + ("" if film else " Open the film first."))
        state = "normal" if film else "disabled"
        opening = app._hosting_now()
        if opening:                      # the tunnel is opening: Host reads Cancel
            self.host_btn.config(text="Cancel", kind="ghost", command=app._cancel_hosting,
                                 state="normal")
            self.join_btn.config(state="disabled")
        else:
            self.host_btn.config(text="Start hosting", kind="primary", command=self._host,
                                 state=state)
            self.join_btn.config(state=state)
            if self._progress:
                self._host_feedback()    # progress from a start that ended

    def refresh_code(self):
        # A host's code is None until the relay answers, seconds after
        # the start; a viewer's is known from the start.
        code = getattr(self.app.ctl.session, "code", None) or "…"
        if code == self._shown_code or self._closed:
            return
        self._shown_code = code
        e = self.code_field
        e.configure(state="normal")
        e.delete(0, "end")
        e.insert(0, code)
        e.configure(state="readonly")

    def _address(self):
        """Where this party's viewers come in: a tunnel's address, else the
        relay the session uses."""
        ctl = self.app.ctl
        return ctl.public_relay or ctl.relay_url

    def _show_address(self):
        addr, tunnel = self._address(), self.app.ctl.public_relay is not None
        e = self.addr_field
        if e.get() != addr:
            e.configure(state="normal")
            e.delete(0, "end")
            e.insert(0, addr)
            e.configure(state="readonly")
        _show(self.addr_note, tunnel, anchor="w", after=self.addr_row, pady=(theme.px(6), 0))
        if tunnel and self.relay_var.get() != addr:
            self.relay_var.set(addr)     # the relay field shows it too, read-only
            self._shown_public = addr

    def set_session_text(self, text):
        if self._closed:
            return
        self.session_lbl.config(text=text)
        self.refresh_code()

    def set_host_note(self, text):
        """The tunnel's progress, under the Host button."""
        if self._closed:
            return
        self._host_feedback(note=text)
        self._progress = True

    def hosting_failed(self, reason, manual):
        """The tunnel did not open. `manual`: cloudflared could not be had,
        so the way to get it by hand goes with the reason."""
        if self._closed:
            return
        self.refresh()
        self._host_feedback(
            error=f"Could not host through this computer: {reason}",
            note=self._MANUAL if manual else None, link=manual)

    _MANUAL = ("Install cloudflared from Cloudflare's download page, then press Start "
               "hosting again - or untick the tunnel to use a relay server.")

    def set_mics(self, names):
        if self._closed:
            return
        self._mics = list(names)
        self.mic_select.set_values([DEFAULT_MIC] + self._mics)
        if self.mic_var.get() not in self._mics:
            # prefer a real mic over BlackHole for the voice stream
            real = [n for n in self._mics if "blackhole" not in n.lower()]
            self.mic_var.set(real[0] if real else self._mics[0] if self._mics else DEFAULT_MIC)

    # --- actions -----------------------------------------------------------

    def _upper_code(self, _event=None):
        e = self.join_code.entry
        text = e.get()
        if "://" in text:
            return                       # a pasted invite: Join takes it apart
        if text != text.upper():
            at = e.index("insert")
            self.join_code.set(text.upper())
            e.icursor(at)

    def _error(self, lbl, btn, text):
        lbl.config(text=text)
        _show(lbl, bool(text), side="bottom", anchor="w", after=btn, pady=(theme.px(12), 0))

    def _host_feedback(self, note=None, link=False, error=None):
        """The lines under the Host button, top to bottom: a note (progress,
        or the way to get cloudflared), the download page's link, an error.
        Packed from the bottom, so each goes in above the one before."""
        px = theme.px
        self._progress = False
        for w in (self.host_err, self.host_link, self.host_note):
            w.pack_forget()
        below = self.host_btn
        if error:
            self.host_err.config(text=error)
            self.host_err.pack(side="bottom", anchor="w", after=below, pady=(px(12), 0))
            below = self.host_err
        if link:
            self.host_link.pack(side="bottom", anchor="w", after=below, pady=(px(4), 0))
            below = self.host_link
        if note:
            self.host_note.config(text=note)
            self.host_note.pack(side="bottom", anchor="w", after=below, pady=(px(12), 0))

    def _relay(self, lbl, btn):
        """The relay address in its field, shown as it will be used - an
        https:// one as wss:// - or None, and why under `btn`, if there
        is none: at once, not after the session's retries run out."""
        url = relay_address(self.relay_var.get())
        self.relay_field.set_error(url is None)
        if url is None:
            self.relay_field.entry.focus_set()
            self._error(lbl, btn, "The relay server needs ws:// or wss://")
            return None
        self.relay_var.set(url)
        return url

    def _host(self):
        app, ctl = self.app, self.app.ctl
        self._host_feedback()
        if not ctl.video_path:
            return
        try:
            delay = float(self.delay_field.get().strip())
        except ValueError:
            delay = None
        if delay is None or not 2 <= delay <= 45:
            self.delay_field.set_error(True)
            self.delay_field.entry.focus_set()
            self._error(self.host_err, self.host_btn, "2 to 45 seconds")
            return
        self.delay_field.set_error(False)
        self.relay_field.set_error(False)
        mic = self.mic_var.get()
        params = dict(password=self.host_pw.get().strip() or None,
                      from_player=self.src_var.get() == "player",
                      mic_name=None if mic == DEFAULT_MIC else (mic or None),
                      delay_hint=delay,
                      title=Path(ctl.video_path).stem)
        ctl.host_via = "tunnel" if self.tunnel_var.get() else "relay"
        if ctl.host_via == "tunnel":
            self._host_here(params)
            return
        relay = self._relay(self.host_err, self.host_btn)
        if relay is None:
            return
        try:
            ctl.host(relay, **params)
        except Exception as e:
            self._error(self.host_err, self.host_btn, f"Could not start: {e}")
            return
        app._session_changed()
        app._save_config()
        self.refresh()

    def _host_here(self, params):
        """Through a free Cloudflare tunnel from this computer: cloudflared
        where it is installed, else downloaded once the user says yes, else
        the way to get it."""
        app = self.app
        exe, _version, _own = app.ctl.find_cloudflared()
        if exe is None and not self._ask_download():
            self._host_feedback(note="A Cloudflare tunnel needs cloudflared. " + self._MANUAL,
                                link=True)
            return
        app._save_config()               # the tunnel choice
        app._host_here(params, exe, exe is None)
        self.refresh()                   # Host reads Cancel
        self.set_host_note("Starting...")

    def _ask_download(self):
        return messagebox.askokcancel(
            "StreamSync - Watch party",
            "Hosting through a free Cloudflare tunnel needs Cloudflare's cloudflared, and it "
            "isn't on this computer. Download it now?",
            detail=(f"StreamSync downloads it from Cloudflare's releases on GitHub (about "
                    f"{hosting.APPROX_MB} MB) into its own folder, and runs it only once "
                    "Cloudflare's published checksum and signature check out."),
            icon="question", parent=self.win)

    def _copy_invite(self):
        """The address and the code, one line to send viewers."""
        code = getattr(self.app.ctl.session, "code", None)
        if not code:
            return                       # the relay has not given one yet
        text = hosting.invite(self._address(), code)
        try:
            self.win.clipboard_clear()
            self.win.clipboard_append(text)
        except tk.TclError:
            return
        self.invite_btn.config(text="Copied")
        self.app.root.after(2000, lambda: self._closed or self.invite_btn.config(
            text="Copy invite"))

    def _join(self):
        app, ctl = self.app, self.app.ctl
        self._error(self.join_err, self.join_btn, "")
        if not ctl.video_path:
            return
        # an invite pasted as the code: its address goes in the relay field
        given = self.join_code.get().strip()
        address, code = hosting.parse_invite(given)
        if address:
            self.relay_var.set(address)
        code = code or given.upper()
        if code != given:
            self.join_code.set(code)
        relay = self._relay(self.join_err, self.join_btn)
        if relay is None:
            return
        try:
            ctl.join(relay, code, self.join_pw.get().strip() or None, app.mute_var.get())
        except Exception as e:
            self._error(self.join_err, self.join_btn, f"Could not start: {e}")
            return
        app._session_changed()
        app._save_config()
        self.refresh()


# --- Update ------------------------------------------------------------------


class UpdateDialog(_Dialog):
    """The offer, or why this copy can't install it; then the progress,
    or what went wrong. Everything it does goes through the app's
    update flow (ui/update.py)."""

    slot = "_update_dialog"
    width = 520
    PAD_X, PAD_Y = 28, 24

    def __init__(self, app, tag, rel, can_install, unavailable_text):
        self.tag, self.rel = tag, rel
        self.can_install, self.unavailable_text = can_install, unavailable_text
        self.ver = tag.lstrip("vV")
        self.view = None
        self._installing = self._cancelling = False
        win = self._open(app, "StreamSync - Update")
        px = theme.px
        outer = _frame(win)
        outer.pack(fill="both", expand=True, padx=px(self.PAD_X), pady=px(self.PAD_Y))
        _frame(outer, width=px(self.width - 2 * self.PAD_X), height=0).pack()
        self.body = _frame(outer)
        self.body.pack(fill="both", expand=True)
        self.buttons = _frame(outer)
        self.buttons.pack(fill="x", pady=(px(14), 0))
        win.protocol("WM_DELETE_WINDOW", self._close_box)
        win.bind("<Escape>", lambda e: self._close_box())
        if can_install:
            self._show_offer()
        else:
            self._show_unavailable()
        self._show_built()

    @property
    def _wrap(self):
        return self.width - 2 * self.PAD_X

    def _clear(self, view):
        # A view's own button can be what changes the view (Install), so
        # the old widgets go once its click has returned.
        self.view = view
        for name in ("body", "buttons"):
            old = getattr(self, name)
            new = _frame(old.master)
            new.pack(after=old, **old.pack_info())
            old.pack_forget()
            _destroy_later(self.app.root, old)
            setattr(self, name, new)

    def _buttons(self, *specs):
        """The view's buttons, given left to right as (text, kind, command):
        they sit at the row's right end, the last one rightmost, as on the
        board. Made in that order, so that Tab reaches them in it too, and
        packed from the right."""
        made = [widgets.FlatButton(self.buttons, text, kind=kind, height=38, padx=18,
                                   font=theme.font(14, kind == "primary"), command=command)
                for text, kind, command in specs]
        for btn in reversed(made):
            btn.pack(side="right", padx=(theme.px(10), 0))
        return made

    def _heading(self, text, icon=False):
        row = _frame(self.body)
        row.pack(fill="x")
        if icon:
            widgets.IconTile(row, "download", 44, radius=10, fg=theme.ACCENT).pack(
                side="left", padx=(0, theme.px(14)))
        _label(row, text, 19, True, wrap=self._wrap - (58 if icon else 0)).pack(side="left")

    def _paragraph(self, text):
        _label(self.body, text, 14, fg=theme.BODY_TEXT, wrap=self._wrap).pack(
            anchor="w", fill="x", pady=(theme.px(14), 0))

    def _show_offer(self):
        app = self.app
        self._clear("offer")
        self._heading(f"StreamSync {self.ver} is ready", icon=True)
        self._paragraph(f"You have {__version__}. The update downloads from GitHub, is checked "
                        "to be a genuine StreamSync release, then installs and restarts the "
                        "app.")
        widgets.FlatButton(self.body, f"What's new in {self.ver}", kind="link", height=22,
                           font=theme.font(13), padx=0, underline=True,
                           command=lambda: app._open_release_notes(self.rel)).pack(
            anchor="w", pady=(theme.px(14), 0))
        _, self.install_btn = self._buttons(
            ("Later", "ghost", self.close),
            ("Install and restart", "primary", lambda: app._start_update(self.tag, self.rel)))

    def _show_unavailable(self):
        self._clear("unavailable")
        self._heading(f"StreamSync {self.ver} is ready", icon=True)
        self._paragraph(f"You have {__version__}. " + self.unavailable_text)
        self._buttons(("Later", "ghost", self.close),
                      ("Open download page", "primary", self._download_page))

    def _show_progress_view(self):
        self._clear("progress")
        self._heading(f"Updating to {self.ver}")
        self.step_lbl = _label(self.body, "Starting...", 14, fg=theme.BODY_TEXT,
                               wrap=self._wrap)
        self.step_lbl.pack(anchor="w", fill="x", pady=(theme.px(14), 0))
        self.progress = widgets.Progress(self.body, height=6)
        self.progress.pack(fill="x", pady=(theme.px(14), 0))
        self.cancel_btn, = self._buttons(("Cancel", "ghost", self._cancel))

    def _download_page(self):
        self.app._open_releases_page()
        self.close()

    # --- what the update flow calls ------------------------------------------

    def show_progress(self, done, total, name):
        if self._closed:
            return
        if self.view != "progress":
            self._show_progress_view()
        if self._cancelling or self._installing:
            return
        if name:
            self.step_lbl.config(text=f"Downloading {name} - {done // 1024 // 1024} MB")
        self.progress.set(done / total if total else None)

    def show_step(self, text):
        if self._closed:
            return
        if self.view != "progress":
            self._show_progress_view()
        self.step_lbl.config(text=text)

    def set_installing(self):
        if self._closed:
            return
        if self.view != "progress":
            self._show_progress_view()
        self._installing = True
        self.progress.set(1.0)
        self.cancel_btn.config(state="disabled")

    def show_failed(self, why):
        if self._closed:
            return
        self._installing = self._cancelling = False
        self._clear("failed")
        self._heading("The update didn't install")
        self._paragraph(f"{why}\n\nYour installed copy has not been changed.")
        self._buttons(("Close", "ghost", self.close),
                      ("Open download page", "primary", self._download_page))

    # --- its own buttons ---------------------------------------------------------

    def _cancel(self):
        # The download stops at its next chunk, and the flow closes this
        # dialog when it has (update_cancelled).
        if self._installing or self._cancelling:
            return
        self._cancelling = True
        self.cancel_btn.config(state="disabled")
        self.step_lbl.config(text="Cancelling...")
        self.app._cancel_update()

    def _close_box(self):
        # Downloading: the close box cancels. Installing: it can no longer
        # stop anything, so it is ignored. Otherwise it closes.
        if self.view == "progress":
            if not self._installing:
                self._cancel()
            return
        self.close()


# --- the gallery -------------------------------------------------------------


def _gallery(argv=None):
    """Open every dialog view over a stub app, for checking against the
    boards: python -m ui.dialogs [--scale 1.5] [--platform mac|win]
    [--view NAME] [--auto] [--texts] [--shots DIR]."""
    import argparse
    import queue
    import subprocess
    import sys
    import types

    views = ("settings", "party", "party-nofilm", "party-live", "update",
             "update-unavailable", "update-progress", "update-failed")
    ap = argparse.ArgumentParser(prog="python -m ui.dialogs")
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--platform", choices=("mac", "win"),
                    default="win" if sys.platform == "win32" else "mac")
    ap.add_argument("--view", choices=views, action="append")
    ap.add_argument("--auto", action="store_true",
                    help="open and close each view by itself, then exit")
    ap.add_argument("--texts", action="store_true", help="print every text each view shows")
    ap.add_argument("--shots", help="save a screenshot of each view here (macOS)")
    args = ap.parse_args(argv)

    if args.platform == "win" and sys.platform != "win32" and "windowctl" not in sys.modules:
        # Only the adapter's constants are read; windowctl calls windll at
        # import, so a stand-in lets them load anywhere.
        sys.modules["windowctl"] = types.ModuleType("windowctl")
    if args.platform == "win":
        from ui import platform_win as platform
    else:
        from ui import platform_mac as platform
    A = platform.Adapter

    class Session:
        def __init__(self, viewer):
            self.code = None if not viewer else "K7QX"

    class Ctl:
        relay_url = "ws://localhost:8765"
        auto_interval = 30
        video_path = "film.mkv"
        session = None

        def session_running(self):
            return self.session is not None

        def host(self, relay, pw, from_player, mic_name, delay_hint, title):
            print(f"ctl.host({relay!r}, {pw!r}, from_player={from_player}, "
                  f"mic_name={mic_name!r}, delay_hint={delay_hint}, title={title!r})")
            self.session = Session(False)

        def join(self, relay, code, pw, mute):
            print(f"ctl.join({relay!r}, {code!r}, {pw!r}, {mute})")
            self.session = session.ViewerSession.__new__(session.ViewerSession)
            self.session.code = code

        def leave(self):
            self.session = None

    class App:
        """Just what the dialogs touch, each action printed."""

        def __init__(self, root):
            self.root = root
            self.q = queue.Queue()
            self.ctl = Ctl()
            for name in ("NAME", "STREAM_LABEL", "STREAM_AUTO_LABEL", "LISTEN_HELP",
                         "DEFAULT_DEVICE_NOTE", "UPDATE_UNAVAILABLE",
                         "REFRESH_STREAMS_ON_SETTINGS"):
                setattr(self, name, getattr(A, name))
            self._settings = self._party = self._update_dialog = None
            self._device_items = ["(default) Speakers", "Speakers", "Headphones"]
            self._stream_choices = ["Twitch - a stream - Browser", "Kick - another - Browser"]
            self._session_text = "Session live - code K7QX. Waiting for viewers."
            self._update_checking = False
            self._update_offer = None
            self._update_line = "StreamSync 1.1.0 is up to date."
            self.player_var = tk.StringVar(root, "embedded")
            self.mute_var = tk.BooleanVar(root, True)
            self.follow_var = tk.BooleanVar(root, True)
            self.interval_var = tk.IntVar(root, 30)
            self.swap_var = tk.BooleanVar(root, True)
            self.device_var = tk.StringVar(root, "(default) Speakers")
            self.stream_var = tk.StringVar(root, "")
            self.hint_var = tk.StringVar(root, "")
            self.check_updates_var = tk.BooleanVar(root, True)

        def __getattr__(self, name):
            if not name.startswith("_") or name.startswith("__"):
                raise AttributeError(name)
            return lambda *a, **k: print(f"app.{name}{a}{k or ''}")

        def _device_label(self, value):
            if value.startswith("(default) "):
                return f"{value[len('(default) '):]} ({self.DEFAULT_DEVICE_NOTE})"
            return value

        def _present(self, win):
            A._present(self, win)

        def _style_window(self, win):
            A._style_window(self, win)

        def _session_running(self):
            return self.ctl.session_running()

        def _session_changed(self):
            if self._party:
                self._party.refresh()

        def _leave_session(self):
            self.ctl.leave()
            self._session_changed()

    def texts(widget, out):
        if isinstance(widget, widgets.Field):
            if widget.get():
                out.append(f"[{widget.get()}]")
            return out
        try:
            t = widget.cget("text")
        except (tk.TclError, ValueError):
            # a drawn Check or Radio keeps its text to itself
            t = getattr(widget, "_text", "") if isinstance(widget, tk.Canvas) else ""
        if t:
            out.append(t)
        if isinstance(widget, tk.Entry):
            if widget.get():
                out.append(f"[{widget.get()}]")
        for child in widget.winfo_children():
            if child.winfo_manager():
                texts(child, out)
        return out

    root = tk.Tk()
    root.title("StreamSync - dialogs gallery")
    theme.init(args.scale, A._ui_font_family(None))
    root.configure(bg=theme.GROUND)
    root.geometry(f"{theme.px(560)}x{theme.px(200)}+40+40")
    app = App(root)
    errors = []
    root.report_callback_exception = lambda *exc: errors.append(exc[1]) or print(
        "Tk callback error:", repr(exc[1]))
    rel = {"tag_name": "v1.1.0", "html_url": "https://github.com/gBear97/StreamSync/releases",
           "assets": []}

    def open_view(name):
        app.ctl.video_path = None if name == "party-nofilm" else "film.mkv"
        app.ctl.session = None
        if name == "settings":
            return SettingsDialog(app)
        if name.startswith("party"):
            if name == "party-live":
                app.ctl.session = Session(False)
            d = WatchPartyDialog(app)
            d.set_mics(["BlackHole 2ch", "MacBook Pro Microphone"])
            if name == "party-live":
                def code_arrives():
                    if not d._closed and app.ctl.session is not None:
                        app.ctl.session.code = "K7QX"
                        d.refresh_code()
                root.after(1500, code_arrives)
            return d
        d = UpdateDialog(app, "v1.1.0", rel, can_install=name != "update-unavailable",
                         unavailable_text=app.UPDATE_UNAVAILABLE)
        if name == "update-progress":
            d.show_progress(37 * 1024 * 1024, 92 * 1024 * 1024, "StreamSync-1.1.0-mac.dmg")
        elif name == "update-failed":
            d.show_failed("Checksum mismatch for StreamSync-1.1.0-mac.dmg - expected "
                          "3f2a..., got 9b1c.... The download was not installed.")
        return d

    order = list(args.view or views)

    def step():
        if not order:
            root.destroy()
            return
        name = order.pop(0)
        d = open_view(name)
        root.update()
        if args.texts:
            print(f"--- {name} ({d.win.title()}, "
                  f"{d.win.winfo_width()}x{d.win.winfo_height()})")
            for t in texts(d.win, []):
                print("   ", t.replace("\n", "\\n"))
        if args.shots:
            Path(args.shots).mkdir(parents=True, exist_ok=True)
            x, y = d.win.winfo_rootx(), d.win.winfo_rooty()
            w, h = d.win.winfo_width(), d.win.winfo_height()
            out = Path(args.shots) / f"{name}-{args.platform}-{int(args.scale * 100)}.png"
            try:
                subprocess.run(["screencapture", "-x", f"-R{x},{y},{w},{h}", str(out)],
                               check=True, timeout=10)
            except (OSError, subprocess.SubprocessError) as e:
                print(f"SKIPPED screenshot {out.name}: {e}")
        if args.auto:
            root.after(300, lambda: (d.close(), step()))
        else:
            d.win.bind("<Destroy>", lambda e: e.widget is d.win and root.after(1, step),
                       add="+")

    root.after(100, step)
    root.mainloop()
    if errors:
        raise SystemExit(f"{len(errors)} Tk callback error(s)")


if __name__ == "__main__":
    _gallery()
