"""Controls drawn to the design, plus fmt_clock.

Aqua's tk.Button ignores colours, and a native check box or combo box
cannot take the design's dark look, so the controls are drawn on Canvases:
the same on Windows and the Mac.

Rules every control here keeps:
- Sizes passed in are design px; each control scales its own with
  theme.px. Fonts come from theme.font, as they are.
- A drawn control's Canvas has highlightthickness and bd 0: Aqua's default
  3-px highlight ring would shift every drawing and add a native-looking
  ring, so each control draws its own focus ring.
- A control's `state` is its own and never reaches the Canvas's -state,
  which would grey out and disable the drawn items. Disabled also takes it
  out of the Tab order.
- Clicks defer: a command runs through after(1, ...), never inside the
  click (PR 31's deadlock rule: a handler that resizes the film window
  from inside a mouse event can hang Windows' modal loop).
- A control bound to a Tk variable traces it, so writes from code show, and
  drops the trace when it is destroyed. Nothing outside this module traces.
"""

import tkinter as tk
import tkinter.font as tkfont

from controller import fmt_time
from ui import icons, theme


def fmt_clock(s):
    """m:ss or h:mm:ss in whole seconds - a timeline's reading."""
    return fmt_time(int(s)).rsplit(".", 1)[0]


def ellipsize(text, font, width_px):
    """`text`, cut to fit `width_px` screen px in `font` with an ellipsis.
    Needs a Tk root: the font is measured at call time."""
    f = tkfont.Font(font=font)
    return _cut(text, f.measure, width_px)


def _cut(text, measure, width_px):
    if measure(text) <= width_px:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure(text[:mid] + "…") <= width_px:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…"


def _bg_of(master):
    try:
        return master.cget("bg")
    except Exception:            # a ttk parent has no -bg
        return theme.GROUND


def round_rect(canvas, x0, y0, x1, y1, r, fill="", outline="", width=1):
    """A rounded rectangle on `canvas` (screen px).

    Made of ovals, arcs, rectangles and lines rather than one polygon: Tk
    snaps a polygon's points to whole pixels - whole points, on a Mac's
    Retina screen - so a 6-px corner came out as a straight cut and a
    switch's round ends as facets, while it draws ovals and arcs smooth."""
    if fill:
        # x1 and y1 are the last pixels inside, as for the outline; Tk fills
        # a rectangle or an oval up to, not including, its far corner
        fx, fy = x1 + 1, y1 + 1
        fr = max(0.0, min(r, (fx - x0) / 2, (fy - y0) / 2))
        if fr < 1:
            canvas.create_rectangle(x0, y0, fx, fy, fill=fill, outline="")
        else:
            d = 2 * fr
            canvas.create_rectangle(x0 + fr, y0, fx - fr, fy, fill=fill, outline="")
            canvas.create_rectangle(x0, y0 + fr, fx, fy - fr, fill=fill, outline="")
            for x, y in ((x0, y0), (fx - d, y0), (x0, fy - d), (fx - d, fy - d)):
                canvas.create_oval(x, y, x + d, y + d, fill=fill, outline="")
    if not outline or outline == fill:   # one in the fill's own colour adds nothing
        return
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    if r < 1:
        canvas.create_rectangle(x0, y0, x1, y1, outline=outline, width=width)
        return
    d = 2 * r
    for x, y, start in ((x1 - d, y0, 0), (x0, y0, 90), (x0, y1 - d, 180), (x1 - d, y1 - d, 270)):
        canvas.create_arc(x, y, x + d, y + d, start=start, extent=90, style="arc",
                          outline=outline, width=width)
    for line in ((x0 + r, y0, x1 - r, y0), (x0 + r, y1, x1 - r, y1),
                 (x0, y0 + r, x0, y1 - r), (x1, y0 + r, x1, y1 - r)):
        canvas.create_line(*line, fill=outline, width=width)


def _later(widget, fn, *args):
    """fn(*args) on the next event-loop pass, unless `widget` is gone by then.

    Scheduled on the root, not on the widget: a command that closes the
    widget's own window (Done, Close) would destroy the widget that owns
    the callback while it runs, and tkinter's clean-up after the callback
    then fails into the Tk hook - or, with the callback's id reused by a
    command made meanwhile, deletes that one instead."""
    def run():
        try:
            alive = widget.winfo_exists()
        except tk.TclError:
            alive = False            # the app has closed
        if alive:
            fn(*args)
    widget._root().after(1, run)


def tab_last(*ws):
    """Make Tab reach these sibling widgets after their other siblings, in
    the order given.

    Tk's Tab goes through siblings in their stacking order, which is the
    order they were made in, not where pack puts them. A widget made early
    so that pack keeps its place - a footer at the bottom, a button at the
    right of its row - would otherwise be reached first. Raising it changes
    nothing on screen where the siblings do not overlap. Called through Tk,
    since a Canvas's own lift() raises canvas items instead."""
    for w in ws:
        w.tk.call("raise", w._w)


class _Traces:
    """Redraw on a variable's writes, and stop when the widget goes."""

    def _watch(self, var, fn):
        self._traces.append((var, var.trace_add("write", lambda *a: fn())))

    def _drop_traces(self, event):
        if str(event.widget) != str(self):
            return               # a child going, not this widget
        for var, name in self._traces:
            try:
                var.trace_remove("write", name)
            except Exception:
                pass
        self._traces = []


class _Drawn(_Traces, tk.Canvas):
    """A control drawn on a Canvas. highlightthickness and bd are 0: Aqua's
    3-px highlight ring would shift every drawing, so the focus ring is
    drawn instead.

    Options named in _OWN are the control's own: configure, cget and
    item access handle them, and everything else is the Canvas's."""

    _OWN = ()

    def __init__(self, master, width, height, takefocus=0, **kw):
        kw.setdefault("bg", _bg_of(master))
        super().__init__(master, width=width, height=height, highlightthickness=0, bd=0,
                         takefocus=takefocus, **kw)
        self._focused = False
        self._traces = []
        self.bind("<Configure>", lambda e: self._redraw(), add="+")
        self.bind("<FocusIn>", lambda e: self._set_focus(True), add="+")
        self.bind("<FocusOut>", lambda e: self._set_focus(False), add="+")
        self.bind("<Destroy>", self._drop_traces, add="+")

    # --- options

    def configure(self, cnf=None, **kw):
        if isinstance(cnf, str):
            if cnf in self._OWN:
                return (cnf, cnf, cnf, None, self.cget(cnf))
            return super().configure(cnf)
        if cnf:
            kw = {**cnf, **kw}
        if not kw:
            if cnf is None:
                found = super().configure()
                found.update({k: (k, k, k, None, self.cget(k)) for k in self._OWN})
                return found
            return None
        own = {k: kw.pop(k) for k in list(kw) if k in self._OWN}
        if kw:
            super().configure(**kw)
        if own:
            self._set_own(own)
        return None

    config = configure

    def cget(self, key):
        if key in self._OWN:
            return self._get_own(key)
        return super().cget(key)

    def __getitem__(self, key):
        return self.cget(key)

    def __setitem__(self, key, value):
        self.configure({key: value})

    def keys(self):
        return super().keys() + list(self._OWN)

    def _get_own(self, key):
        return getattr(self, "_" + key)

    def _set_own(self, changes):
        for key, value in changes.items():
            setattr(self, "_" + key, value)
        self._redraw()

    # --- drawing

    def _set_focus(self, on):
        self._focused = on
        self._redraw()

    def _redraw(self):
        try:
            self._draw()
        except tk.TclError:
            pass                 # destroyed meanwhile

    def _draw(self):
        pass

    def _wh(self):
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:     # not laid out yet: what it asked for
            w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        return w, h

    def _measure(self, text, font):
        if not text:
            return 0
        return int(self.tk.call("font", "measure", font, "-displayof", self._w, text))

    def _linespace(self, font):
        return int(self.tk.call("font", "metrics", font, "-displayof", self._w, "-linespace"))

    def _fit_text(self, text, font, width_px):
        if width_px <= 0:
            return ""
        return _cut(text, lambda t: self._measure(t, font), width_px)

    @staticmethod
    def _ring_width():
        return max(2, theme.px(2))


class _Control(_Drawn):
    """A drawn control that takes clicks and the keyboard.

    `state` ("normal" or "disabled") and `command` are its own options.
    Disabled draws the disabled look, shows the plain cursor and takes the
    control out of the Tab order; enabling restores the constructor's
    takefocus and cursor. A click (press and release over the control),
    Return or KP_Enter activates it; Space does not, as it is the pause
    key."""

    _OWN = ("state", "command", "takefocus", "cursor")

    def __init__(self, master, width, height, *, command=None, takefocus=True,
                 state="normal", cursor="hand2", **kw):
        self._command, self._state = command, state
        self._takefocus, self._cursor = takefocus, cursor
        self._hover = self._pressed = False
        super().__init__(master, width, height, takefocus=self._focus_flag(),
                         cursor=self._cursor_now(), **kw)
        self.bind("<Enter>", lambda e: self._set_hover(True), add="+")
        self.bind("<Leave>", lambda e: self._set_hover(False), add="+")
        self.bind("<ButtonPress-1>", self._press, add="+")
        self.bind("<ButtonRelease-1>", self._release, add="+")
        self.bind("<Return>", lambda e: self._key(), add="+")
        self.bind("<KP_Enter>", lambda e: self._key(), add="+")

    def _enabled(self):
        return self._state != "disabled"

    def _focus_flag(self):
        return 1 if self._takefocus and self._enabled() else 0

    def _cursor_now(self):
        return self._cursor if self._enabled() else ""

    def _set_own(self, changes):
        if all(getattr(self, "_" + k) == v for k, v in changes.items()):
            return               # a tick re-setting the same text: nothing to redraw
        for key, value in changes.items():
            setattr(self, "_" + key, value)
        if changes.keys() & {"state", "takefocus", "cursor"}:
            tk.Canvas.configure(self, takefocus=self._focus_flag(), cursor=self._cursor_now())
            if not self._enabled():
                self._pressed = False
        self._changed(changes)

    def _changed(self, changes):
        self._redraw()

    def _defer(self, fn, *args):
        if fn is not None:
            _later(self, fn, *args)

    def _set_hover(self, on):
        self._hover = on
        self._redraw()

    def _press(self, event):
        if self._enabled():
            self._pressed = True
            self._redraw()

    def _release(self, event):
        was, self._pressed = self._pressed, False
        self._redraw()
        w, h = self._wh()
        if was and self._enabled() and 0 <= event.x < w and 0 <= event.y < h:
            self._activate()

    def _key(self):
        if self._enabled():
            self._activate()
        return "break"

    def _activate(self):
        self.invoke()

    def invoke(self):
        """What a click does; does nothing while disabled."""
        if self._enabled():
            self._defer(self._command)


# kind -> theme names of (fill, text, hover fill, pressed fill, outline) and
# the corner radius in design px; None is no fill (the parent's background)
# or no outline. A round button is a circle.
_KINDS = {
    "primary": (("ACCENT", "ON_ACCENT", "ACCENT_HOVER", "ACCENT_PRESSED", None), 6),
    "secondary": (("RAISED", "TEXT", "RAISED_HOVER", "RAISED", "LINE"), 6),
    "ghost": ((None, "TEXT", "RAISED", "RAISED_HOVER", "LINE"), 6),
    "link": ((None, "LINK", None, None, None), 4),
    "round": (("PLAY_BG", "ON_ACCENT", "KNOB", "MUTED", None), None),
    "bar": (("BAR_BTN", "BAR_FG", "BAR_BTN_HOVER", "BAR_BTN", None), 0),
    "chip": (("RAISED", "TEXT", "RAISED_HOVER", "RAISED", "LINE"), 6),
}
_PAD = {"primary": 18, "secondary": 12, "ghost": 12, "link": 4, "bar": 12, "chip": 9,
        "round": 0}              # default padx, design px
_GAP = {"bar": 6}                # icon to text, design px; else 7


class FlatButton(_Control):
    """A button: Aqua's tk.Button ignores colours.

    Kinds: primary (accent), secondary (raised, outlined), ghost (outlined
    only), link, round (the play button: a circle, icon only), bar (the
    fullscreen bar's) and chip (Fine-tune's small steps).

    invoke() runs the command on the next event-loop pass, never inside
    the click. `state` is the button's own and never reaches the Canvas's
    -state; disabled also takes it out of the Tab order. Return and
    KP_Enter invoke a focused button; Space does not, as it is the pause
    key. A focused button draws a ring; a link with no room beside its
    text for one (padx 0, in a sentence) thickens its underline instead.

    Beyond the design's options: `padx` (the inner padding each side),
    `icon_size`, `outline` (a colour drawn round any kind: the main
    window's "Film sound on" warning) and `underline` (the text of a link
    that sits in a sentence, as the boards' links are) - all optional.
    configure() and cget() handle text, icon, kind, state, command and
    outline."""

    _OWN = _Control._OWN + ("text", "icon", "kind", "outline")

    def __init__(self, master, text="", *, icon=None, icon_after=False, kind="secondary",
                 height=36, width=None, font=None, command=None, takefocus=True,
                 state="normal", padx=None, icon_size=None, outline=None, underline=False,
                 **kw):
        if kind not in _KINDS:
            raise ValueError(f"no FlatButton kind {kind!r}")
        self._text, self._icon, self._icon_after = text, icon, icon_after
        self._kind, self._outline, self._underline = kind, outline, underline
        self._height, self._width, self._padx = height, width, padx
        self._icon_size = icon_size
        self._font_given = font
        super().__init__(master, 1, theme.px(height), command=command, takefocus=takefocus,
                         state=state, cursor=kw.pop("cursor", "hand2"), **kw)
        self._fit()

    def _changed(self, changes):
        if changes.keys() & {"text", "icon", "kind"}:
            self._fit()
        else:
            self._redraw()

    def _set_own(self, changes):
        if changes.get("kind", self._kind) not in _KINDS:
            raise ValueError(f"no FlatButton kind {changes['kind']!r}")
        super()._set_own(changes)

    def _font(self):
        if self._font_given:
            return self._font_given
        if self._kind == "chip":
            return theme.font(13)
        return theme.font(14, self._kind == "primary")

    def _pad(self):
        return theme.px(self._padx if self._padx is not None else _PAD[self._kind])

    def _icon_px(self):
        if not self._icon:
            return 0
        if self._icon_size is not None:
            size = self._icon_size
        elif self._kind == "bar":
            size = 15
        elif self._kind == "round":
            size = round(self._height * 18 / 44)
        else:
            size = 18 if self._height >= 44 else 16
        return theme.px(size)

    def _gap(self):
        if self._icon and self._text:
            return theme.px(_GAP.get(self._kind, 7))
        return 0

    def _fit(self):
        if self._width is not None:
            width = theme.px(self._width)
        elif self._kind == "round":
            width = theme.px(self._height)
        else:
            width = (self._measure(self._text, self._font()) + self._icon_px() + self._gap()
                     + 2 * self._pad())
        tk.Canvas.configure(self, width=max(1, width))
        self._redraw()

    def _look(self):
        """(fill, text colour, outline) for the current state."""
        (bg, fg, hover, pressed, line), _ = _KINDS[self._kind]
        if not self._enabled():
            if self._kind == "bar":
                return theme.BAR_BTN, theme.DISABLED_FG, ""
            # the boards' disabled button: the raised fill, with its own
            # faint outline whatever the kind had
            return (theme.DISABLED_BG if bg else "", theme.DISABLED_FG,
                    theme.DISABLED_LINE if bg or line or self._outline else "")
        name = (pressed if self._pressed and self._hover and pressed
                else hover if self._hover and hover else bg)
        fill = getattr(theme, name) if name else ""
        color = getattr(theme, fg)
        if self._kind == "link" and (self._hover or self._focused):
            # brighter under the pointer and with the keyboard; its own
            # colour again while pressed
            color = theme.LINK if self._pressed and self._hover else theme.LINK_HOVER
        outline = self._outline or (getattr(theme, line) if line else "")
        return fill, color, outline

    def _draw(self):
        self.delete("all")
        w, h = self._wh()
        fill, color, outline = self._look()
        _, radius = _KINDS[self._kind]
        if self._kind == "round":
            self.create_oval(0, 0, w - 1, h - 1, fill=fill, outline=outline or fill)
        elif fill or outline:
            round_rect(self, 0, 0, w - 1, h - 1, theme.px(radius), fill=fill, outline=outline,
                       width=max(1, theme.px(1)))
        font = self._font()
        icon_w, gap = self._icon_px(), self._gap()
        room = w - 2 * self._pad() - icon_w - gap
        text = self._fit_text(self._text, font, room) if self._text else ""
        text_w = self._measure(text, font)
        x = (w - (text_w + icon_w + gap)) // 2
        iy = (h - icon_w) // 2
        focused = self._focused and self._enabled()
        # A link that sits in its sentence (padx 0) has no room beside its
        # text for a ring, which would be drawn over its first and last
        # letters: with the keyboard, its underline thickens instead.
        thick_line = focused and self._kind == "link" and x <= self._ring_width()
        if self._icon and not self._icon_after:
            icons.draw(self, self._icon, x, iy, icon_w, color)
            x += icon_w + gap
        if text:
            if thick_line:
                self._thick_underline(x, text_w, h, font, color)
            elif self._underline and isinstance(font, tuple):
                font = tuple(font) + ("underline",)     # same width: only the line is added
            self.create_text(x, h // 2, text=text, fill=color, font=font, anchor="w")
            x += text_w + gap
        if self._icon and self._icon_after:
            icons.draw(self, self._icon, x, iy, icon_w, color)
        if focused and not thick_line:
            # the ring stands out from the fill it sits on
            ring = theme.TEXT if self._kind == "primary" else theme.ACCENT
            rw = self._ring_width()
            if self._kind == "round":
                self.create_oval(1, 1, w - 2, h - 2, outline=ring, width=rw)
            else:
                round_rect(self, 1, 1, w - 2, h - 2, max(0, theme.px(radius) - 1),
                           outline=ring, width=rw)

    def _thick_underline(self, x, width, h, font, color):
        """An inline link's focus mark: an underline as thick as a focus
        ring, under the baseline of the text that sits on the canvas's
        middle row, in place of the font's own thin one."""
        rw = self._ring_width()
        ascent = int(self.tk.call("font", "metrics", font, "-displayof", self._w, "-ascent"))
        # where Tk puts the top of a one-line text anchored "w" at h // 2
        top = h // 2 - self._linespace(font) // 2
        y = min(top + ascent + max(1, theme.px(1)), h - rw)
        self.create_rectangle(x, y, x + width, y + rw, fill=color, outline="")


class _Toggle(_Control):
    """A control bound to a variable: a click or Return sets the variable,
    then runs `command` deferred. It redraws from the variable's trace, so
    writes from code show."""

    def __init__(self, master, variable, width, height, *, command=None, **kw):
        self._var = variable
        super().__init__(master, width, height, command=command, **kw)
        self._watch(variable, self._redraw)

    def _activate(self):
        self.invoke()

    def invoke(self):
        """What a click does; does nothing while disabled."""
        if not self._enabled():
            return
        if self._set():
            self._defer(self._command)

    def _set(self):
        """Write the variable; True when the command should run."""
        self._var.set(not self._on())
        return True

    def _on(self):
        try:
            return bool(self._var.get())
        except (tk.TclError, ValueError):
            return False


class Switch(_Toggle):
    """An on/off switch bound to a BooleanVar: 40x22, knob 16, padding 3.
    On: an accent track with a dark knob. Off: a LINE track with a FAINT
    knob."""

    def __init__(self, master, variable, *, command=None, **kw):
        super().__init__(master, variable, theme.px(40), theme.px(22), command=command,
                         cursor=kw.pop("cursor", "hand2"), **kw)
        self._redraw()

    def _draw(self):
        self.delete("all")
        w, h = self._wh()
        on, enabled = self._on(), self._enabled()
        pad, knob = theme.px(3), theme.px(16)
        if enabled:
            track = (theme.ACCENT_HOVER if self._hover else theme.ACCENT) if on else theme.LINE
            dot = theme.ON_ACCENT if on else theme.FAINT
        else:
            track = theme.DISABLED_FG if on else theme.DISABLED_BG
            dot = theme.DISABLED_BG if on else theme.DISABLED_FG
        round_rect(self, 0, 0, w - 1, h - 1, h / 2, fill=track, outline=track)
        x = w - pad - knob if on else pad
        y = (h - knob) // 2
        self.create_oval(x, y, x + knob - 1, y + knob - 1, outline="", fill=dot)
        if self._focused and enabled:
            round_rect(self, 1, 1, w - 2, h - 2, (h - 2) / 2,
                       outline=theme.TEXT if on else theme.ACCENT, width=self._ring_width())


class Check(_Toggle):
    """A check box with its label and an optional muted sub-label; the whole
    row is clickable. Bound to a BooleanVar."""

    BOX = 16                     # design px
    _OWN = _Toggle._OWN + ("text", "sub")

    def __init__(self, master, text, variable, *, command=None, sub=None, **kw):
        self._text, self._sub = text, sub
        super().__init__(master, variable, 1, 1, command=command,
                         cursor=kw.pop("cursor", "hand2"), **kw)
        self._fit()

    def _changed(self, changes):
        if changes.keys() & {"text", "sub"}:
            self._fit()
        else:
            self._redraw()

    def _fit(self):
        main, small = theme.font(14), theme.font(12)
        box = theme.px(self.BOX)
        text_w = max(self._measure(self._text, main), self._measure(self._sub, small))
        h = self._linespace(main)
        if self._sub:
            h += theme.px(1) + self._linespace(small)
        tk.Canvas.configure(self, width=box + theme.px(10) + text_w + 1, height=max(box, h))
        self._redraw()

    def _box_colours(self):
        """(fill, outline, mark) of the box."""
        on = self._on()
        if not self._enabled():
            return ((theme.DISABLED_FG, theme.DISABLED_FG, theme.DISABLED_BG) if on
                    else (theme.DISABLED_BG, theme.DISABLED_LINE, ""))
        if on:
            fill = theme.ACCENT_HOVER if self._hover else theme.ACCENT
            return fill, fill, theme.ON_ACCENT
        # FAINT, not LINE: a LINE outline all but vanishes on a card
        # (about 1.4:1), and a control's edge needs 3:1 to be seen
        return theme.FIELD, theme.MUTED if self._hover else theme.FAINT, ""

    def _mark(self, x, y, box):
        fill, outline, mark = self._box_colours()
        round_rect(self, x, y, x + box - 1, y + box - 1, theme.px(4), fill=fill,
                   outline=outline, width=max(1, theme.px(1)))
        if mark:
            u = box / 16
            self.create_line(x + 4 * u, y + 8.5 * u, x + 7 * u, y + 11.5 * u,
                             x + 12 * u, y + 5 * u, fill=mark, width=max(1.5, 2 * u),
                             capstyle="round", joinstyle="round")
        if self._focused and self._enabled():
            round_rect(self, x, y, x + box - 1, y + box - 1, theme.px(4), outline=(
                theme.TEXT if self._on() else theme.ACCENT), width=self._ring_width())

    def _draw(self):
        self.delete("all")
        w, h = self._wh()
        box = theme.px(self.BOX)
        main = theme.font(14)
        line = self._linespace(main)
        if self._sub:
            top = max(0, (line - box) // 2)       # the box sits on the first line
        else:
            top = max(0, (h - box) // 2)
        self._mark(0, top, box)
        x = box + theme.px(10)
        fg = theme.TEXT if self._enabled() else theme.DISABLED_FG
        if self._sub:
            self.create_text(x, 0, text=self._text, anchor="nw", font=main, fill=fg)
            self.create_text(x, line + theme.px(1), text=self._sub, anchor="nw",
                             font=theme.font(12),
                             fill=theme.MUTED if self._enabled() else theme.DISABLED_FG)
        else:
            self.create_text(x, h // 2, text=self._text, anchor="w", font=main, fill=fg)


class Radio(Check):
    """One choice of several sharing a variable: sets it to `value`. A click
    on the choice already made changes nothing and runs no command."""

    def __init__(self, master, text, variable, value, *, command=None, sub=None, **kw):
        self._value = value
        super().__init__(master, text, variable, command=command, sub=sub, **kw)

    def _on(self):
        try:
            return self._var.get() == self._value
        except (tk.TclError, ValueError):
            return False

    def _set(self):
        if self._on():
            return False
        self._var.set(self._value)
        return True

    def _mark(self, x, y, box):
        fill, outline, mark = self._box_colours()
        self.create_oval(x, y, x + box - 1, y + box - 1, fill=fill, outline=outline,
                         width=max(1, theme.px(1)))
        if mark:
            # chosen, as on the boards: an accent ring and an accent dot,
            # with a dark gap between them
            c = (box - 1) / 2
            for r, colour in ((c - max(1.0, box * 0.08), mark), (box * 0.27, fill)):
                self.create_oval(x + c - r, y + c - r, x + c + r, y + c + r, fill=colour,
                                 outline="")
        if self._focused and self._enabled():
            self.create_oval(x, y, x + box - 1, y + box - 1, width=self._ring_width(),
                             outline=theme.TEXT if self._on() else theme.ACCENT)


class Timeline(_Drawn):
    """A seek bar: rest track, played part and knob.

    Generalised from the fullscreen bar's track. set(frac or None) is
    ignored while dragging. A press is ignored when can_seek() is falsy; a
    drag calls on_preview(frac); the release calls on_seek(frac) once, on
    the next event-loop pass. `inset` is the scaled inset, in screen px."""

    def __init__(self, master, *, bg=None, rest=None, played=None, knob=None, knob_r=7,
                 thickness=4, inset=8, height=22, width=40, on_preview=None, on_seek=None,
                 can_seek=None, **kw):
        self._rest, self._played, self._knob = rest, played, knob
        self._knob_r, self._thickness = theme.px(knob_r), max(1, theme.px(thickness))
        self.inset = theme.px(inset)
        self._on_preview, self._on_seek, self._can_seek = on_preview, on_seek, can_seek
        self.dragging = False
        self._frac = None
        if bg is not None:
            kw["bg"] = bg
        kw.setdefault("cursor", "hand2")
        # a small width of its own: the track takes what pack hands it,
        # rather than asking for 10cm and squeezing the film length out
        super().__init__(master, theme.px(width), theme.px(height), takefocus=0, **kw)
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)

    def set(self, frac):
        if self.dragging:
            return
        if frac is not None:
            frac = min(1.0, max(0.0, frac))
        if frac == self._frac:
            return
        self._frac = frac
        self._redraw()

    def frac_at(self, x):
        w = self.winfo_width()
        return min(1.0, max(0.0, (x - self.inset) / max(1, w - 2 * self.inset)))

    def cancel_drag(self):
        self.dragging = False

    def _press(self, event):
        if self._can_seek is not None and not self._can_seek():
            return
        self.dragging = True
        self._drag(event)

    def _drag(self, event):
        if not self.dragging:
            return
        self._frac = self.frac_at(event.x)
        self._redraw()
        if self._on_preview is not None:
            self._on_preview(self._frac)

    def _release(self, event):
        # the knob follows the drag; the film moves once, on release
        if not self.dragging:
            return
        self.dragging = False
        self._frac = frac = self.frac_at(event.x)
        self._redraw()
        if self._on_seek is not None:
            _later(self, self._on_seek, frac)

    def _draw(self):
        self.delete("all")
        w, h = self._wh()
        y = h // 2
        x0, x1 = self.inset, max(self.inset, w - self.inset)
        # The track runs the whole width, its round ends inside it, as the
        # boards' sliders do; the knob's centre stays `inset` in from
        # either end, so the knob is never cut off.
        cap = self._thickness / 2
        self.create_line(cap, y, max(cap, w - cap), y, width=self._thickness,
                         capstyle="round", fill=self._rest or theme.LINE)
        if self._frac is not None:
            x = x0 + (x1 - x0) * self._frac
            r = self._knob_r
            self.create_line(cap, y, max(cap, x), y, width=self._thickness, capstyle="round",
                             fill=self._played or theme.ACCENT)
            self.create_oval(x - r, y - r, x + r, y + r, outline="",
                             fill=self._knob or theme.KNOB)


class Scrollbar(_Drawn):
    """A vertical scrollbar: a thin rounded thumb on its parent's
    background, brighter under the pointer and while it is dragged. A
    native scrollbar ignores colours (light grey on Windows, the system's
    look on the Mac) and would run a light strip down a dark window.

    It speaks Tk's scrollbar protocol, so it stands where a tk.Scrollbar
    would: the scrolled widget's yscrollcommand calls set(first, last), and
    `command` - the widget's yview - is called as ("moveto", fraction) by a
    drag and as ("scroll", -1 or 1, "pages") by a click on the track above
    or below the thumb. Like every drawn control's, the command runs on the
    next pass, never inside the mouse event. It is never a Tab stop: the
    wheel scrolls the area, and an area that follows the focus shows the
    control Tab reaches."""

    def __init__(self, master, *, command=None, width=12, **kw):
        self._command = command
        self._first, self._last = 0.0, 1.0
        self._hover = False
        self._held = None        # (pointer y, first) from a press on the thumb to its release
        super().__init__(master, theme.px(width), theme.px(4 * width), **kw)
        self.bind("<Enter>", lambda e: self._set_hover(True), add="+")
        self.bind("<Leave>", lambda e: self._set_hover(False), add="+")
        self.bind("<ButtonPress-1>", self._press, add="+")
        self.bind("<B1-Motion>", self._drag, add="+")
        self.bind("<ButtonRelease-1>", self._release, add="+")
        self._redraw()

    def set(self, first, last):
        """What the scrolled widget shows, as fractions of its height."""
        first, last = float(first), float(last)
        if (first, last) != (self._first, self._last):
            self._first, self._last = first, last
            self._redraw()

    def get(self):
        return self._first, self._last

    def _set_hover(self, on):
        self._hover = on
        self._redraw()

    def _thumb(self):
        """(top, bottom, travel): the thumb's ends and how far it can move
        along the track, in screen px."""
        w, h = self._wh()
        pad = theme.px(2)
        track = max(1, h - 2 * pad)
        span = min(1.0, max(0.0, self._last - self._first))
        size = min(track, max(2 * w, round(track * span)))
        travel = track - size
        rest = 1.0 - span
        # the view can stop past the end by less than a scroll step, so
        # the thumb is held at the bottom
        at = min(1.0, max(0.0, self._first / rest)) if rest > 0 else 0.0
        top = pad + round(travel * at)
        return top, top + size, travel

    def _press(self, event):
        top, bottom, _ = self._thumb()
        if top <= event.y < bottom:
            self._held = (event.y, self._first)
            self._redraw()
        elif self._command is not None and self._last - self._first < 1.0:
            # a page towards the click, as a native track does
            _later(self, self._command, "scroll", -1 if event.y < top else 1, "pages")

    def _drag(self, event):
        if self._held is None or self._command is None:
            return
        y0, first0 = self._held
        travel = self._thumb()[2]
        rest = 1.0 - (self._last - self._first)
        if travel > 0 and rest > 0:
            first = first0 + (event.y - y0) / travel * rest
            _later(self, self._command, "moveto", min(rest, max(0.0, first)))

    def _release(self, event):
        if self._held is not None:
            self._held = None
            self._redraw()

    def _draw(self):
        self.delete("all")
        if self._last - self._first >= 1.0:
            return               # all of it shows: nothing to scroll
        w, _ = self._wh()
        top, bottom, _ = self._thumb()
        thick = max(2, theme.px(6))
        x0 = (w - thick) // 2
        color = theme.FAINT if self._hover or self._held is not None else theme.LINE
        round_rect(self, x0, top, x0 + thick - 1, bottom - 1, thick / 2, fill=color,
                   outline=color)


class Select(_Control):
    """A drop-down: label_for(value) and a chevron; a click pops a menu of
    the values under it. choose(value) is what picking one does: it sets
    the variable and runs `command` deferred. Redraws from the variable's
    trace. `state` works as on FlatButton (the Settings stream row is
    disabled while the swap is off)."""

    def __init__(self, master, variable, *, values=(), label_for=None, command=None,
                 height=36, width=None, **kw):
        self._var, self._values = variable, list(values)
        self._label_for = label_for
        self._menu = None
        # a small width of its own, like Timeline's: fill="x" gives it the rest
        super().__init__(master, theme.px(width or 160), theme.px(height), command=command,
                         cursor=kw.pop("cursor", "hand2"), **kw)
        self.bind("<Down>", lambda e: self._key(), add="+")
        self._watch(variable, self._redraw)
        self._redraw()

    def set_values(self, seq):
        self._values = list(seq)
        self._redraw()

    def choose(self, value):
        self._var.set(value)
        self._defer(self._command)

    def invoke(self):
        """Pop the menu on the next pass; does nothing while disabled."""
        if self._enabled():
            self._defer(self._popup)

    def _label(self, value):
        if self._label_for is None:
            return str(value)
        try:
            return str(self._label_for(value))
        except Exception:
            return str(value)

    def _popup(self):
        if not self._values or not self._enabled():
            return
        if self._menu is None:
            self._menu = tk.Menu(self, tearoff=0)
        m = self._menu
        m.delete(0, "end")
        for value in self._values:
            m.add_radiobutton(label=self._label(value), value=value, variable=self._var,
                              command=lambda v=value: self.choose(v))
        try:
            m.tk_popup(self.winfo_rootx(), self.winfo_rooty() + self.winfo_height())
        finally:
            m.grab_release()

    def _draw(self):
        self.delete("all")
        w, h = self._wh()
        enabled = self._enabled()
        if enabled:
            fill, line, fg, arrow = theme.FIELD, theme.LINE, theme.TEXT, theme.MUTED
        else:
            fill, line = theme.DISABLED_BG, theme.DISABLED_LINE
            fg = arrow = theme.DISABLED_FG
        lw = max(1, theme.px(1))
        if self._focused and enabled:
            line, lw = theme.ACCENT, self._ring_width()
        round_rect(self, 0, 0, w - 1, h - 1, theme.px(6), fill=fill, outline=line, width=lw)
        pad, size = theme.px(10), theme.px(14)
        font = theme.font(14)
        try:
            label = self._label(self._var.get())
        except (tk.TclError, ValueError):
            label = ""
        label = self._fit_text(label, font, w - 2 * pad - size - theme.px(6))
        self.create_text(pad, h // 2, text=label, anchor="w", font=font, fill=fg)
        icons.draw(self, "chevron", w - pad - size, (h - size) // 2, size, arrow)


class _Backdrop(tk.Canvas):
    """The rounded box behind a Card's or a Field's content. A tk.Frame is
    square, so the frame takes its parent's colour and this Canvas, placed
    under everything packed into the frame, draws the box with the boards'
    rounded corners. The corners must fall in the frame's padding, where
    nothing is packed over them."""

    def __init__(self, frame, fill, outline, radius):
        self._fill, self._outline, self._radius = fill, outline, radius
        super().__init__(frame, bg=frame.cget("bg"), highlightthickness=0, bd=0, takefocus=0)
        # made before the frame's content, so it stacks under all of it;
        # placed, so it never changes the size the content asks for
        self.place(x=0, y=0, relwidth=1, relheight=1)
        self.bind("<Configure>", lambda e: self._draw(), add="+")

    def recolour(self, outline):
        if outline != self._outline:
            self._outline = outline
            self._draw()

    def _draw(self):
        try:
            self.delete("all")
            w, h = self.winfo_width(), self.winfo_height()
            if w > 1 and h > 1:
                round_rect(self, 0, 0, w - 1, h - 1, self._radius, fill=self._fill,
                           outline=self._outline, width=max(1, theme.px(1)))
        except tk.TclError:
            pass                 # destroyed meanwhile


class Field(_Traces, tk.Frame):
    """A text field: .entry is the tk.Entry. The placeholder shows over an
    empty, unfocused entry; set_error() gives it a red border.

    The box and its border are drawn behind the entry (_Backdrop), so the
    text can sit 10 px in from the border as on the boards - tk.Entry has
    no padding of its own - and the corners round as theirs do. The
    border turns accent while the entry has the keyboard. The frame's
    highlight colours, though not drawn, follow the border's, as does the
    entry's, so either reads what the field shows."""

    def __init__(self, master, *, textvariable=None, width=None, height=36, placeholder="",
                 show=None, font=None, **kw):
        kw.setdefault("bg", _bg_of(master))     # outside the rounded corners
        line = max(1, theme.px(1))
        super().__init__(master, width=theme.px(width or 120), height=theme.px(height),
                         highlightthickness=0, highlightbackground=theme.LINE,
                         highlightcolor=theme.LINE, bd=0, takefocus=0, **kw)
        self.pack_propagate(False)
        self._back = _Backdrop(self, theme.FIELD, theme.LINE, theme.px(6))
        self._traces = []
        self._focused = self._error = False
        self._var = textvariable if textvariable is not None else tk.StringVar(self)
        font = font or theme.font(14)
        self.entry = tk.Entry(self, textvariable=self._var, bg=theme.FIELD, fg=theme.TEXT,
                              insertbackground=theme.TEXT, disabledbackground=theme.FIELD,
                              readonlybackground=theme.FIELD,
                              disabledforeground=theme.DISABLED_FG,
                              selectbackground=theme.LINE, selectforeground=theme.TEXT,
                              relief="flat", bd=0, highlightthickness=0,
                              highlightbackground=theme.LINE, highlightcolor=theme.LINE,
                              font=font, show=show or "")
        # inside the border: 10 px in from it at the sides, clear of it above and below
        self.entry.pack(fill="both", expand=True, padx=theme.px(10), pady=line)
        for w in (self, self._back):
            w.bind("<Button-1>", lambda e: self.entry.focus_set())
        self._placeholder = None
        if placeholder:
            self._placeholder = tk.Label(self, text=placeholder, bg=theme.FIELD,
                                         fg=theme.FAINT, font=font, bd=0, padx=0, pady=0)
            self._placeholder.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.entry.bind("<FocusIn>", lambda e: self._focus(True), add="+")
        self.entry.bind("<FocusOut>", lambda e: self._focus(False), add="+")
        self.bind("<Destroy>", self._drop_traces, add="+")
        self._watch(self._var, self._show_placeholder)
        self._show_placeholder()

    def get(self):
        return self.entry.get()

    def set(self, text):
        self._var.set(text)

    def set_error(self, on):
        self._error = bool(on)
        self._border()

    def _border(self):
        color = (theme.ERROR if self._error else theme.ACCENT if self._focused
                 else theme.LINE)
        try:
            self._back.recolour(color)
            # the frame and the entry carry the same colours, undrawn (their
            # highlights are 0 px), so a check of either reads what shows
            self.configure(highlightbackground=color, highlightcolor=color)
            self.entry.configure(highlightbackground=color, highlightcolor=color)
        except tk.TclError:
            pass                 # destroyed meanwhile

    def _focus(self, on):
        self._focused = on
        self._border()
        self._show_placeholder()

    def _show_placeholder(self):
        if self._placeholder is None:
            return
        try:
            # the variable, not the entry: this trace can run before the
            # entry's own, while the entry still shows the old text
            if self._focused or self._var.get():
                self._placeholder.place_forget()
            else:
                self._placeholder.place(in_=self.entry, x=0, rely=0.5, anchor="w")
        except tk.TclError:
            pass                 # destroyed meanwhile


class Card(tk.Frame):
    """A card: CARD with a 1-px CARD_LINE border and rounded corners
    (`radius`, design px; drawn by a _Backdrop). Put its content in .body,
    which carries the padding."""

    def __init__(self, master, *, padding=18, radius=8, **kw):
        color = kw.pop("bg", theme.CARD)
        kw.setdefault("bg", _bg_of(master))     # outside the rounded corners
        super().__init__(master, highlightthickness=0, bd=0, **kw)
        self._back = _Backdrop(self, color, theme.CARD_LINE, theme.px(radius))
        self.body = tk.Frame(self, bg=color)
        self.body.pack(fill="both", expand=True, padx=theme.px(padding),
                       pady=theme.px(padding))


class Dot(_Drawn):
    """A state dot; set(color) recolours it."""

    def __init__(self, master, color, *, size=9, **kw):
        self._color = color
        super().__init__(master, theme.px(size), theme.px(size), **kw)
        self._redraw()

    def set(self, color):
        if color != self._color:
            self._color = color
            self._redraw()

    def _draw(self):
        self.delete("all")
        w, h = self._wh()
        self.create_oval(0, 0, w - 1, h - 1, fill=self._color, outline=self._color)


class IconTile(_Drawn):
    """An icon on a rounded tile, the icon half the tile's size. `bg` is the
    tile's colour (TILE by default); `command`, when given, runs deferred
    on a click."""

    def __init__(self, master, icon, size=44, *, radius=10, fg=None, bg=None, command=None,
                 **kw):
        self._icon, self._radius, self._fg, self._tile = icon, theme.px(radius), fg, bg
        self._command = command
        super().__init__(master, theme.px(size), theme.px(size), **kw)
        if command is not None:
            self.bind("<ButtonRelease-1>", self._click)
        self._redraw()

    def _click(self, event):
        w, h = self._wh()
        if 0 <= event.x < w and 0 <= event.y < h:
            _later(self, self._command)

    def _draw(self):
        self.delete("all")
        w, h = self._wh()
        tile = self._tile or theme.TILE
        round_rect(self, 0, 0, w - 1, h - 1, self._radius, fill=tile, outline=tile)
        size = round(w / 2)
        icons.draw(self, self._icon, (w - size) / 2, (h - size) / 2, size,
                   self._fg or theme.TEXT)


class Kbd(_Drawn):
    """A key cap, for the Keys section: FIELD with a LINE border, its text
    at least 54 wide with 6 either side and 3 above and below, as the
    board's."""

    def __init__(self, master, text, **kw):
        self._text = text
        super().__init__(master, 1, 1, **kw)
        font = theme.font(13)
        line = max(1, theme.px(1))
        tk.Canvas.configure(
            self, width=max(theme.px(54), self._measure(text, font)) + 2 * (theme.px(6) + line),
            height=self._linespace(font) + 2 * (theme.px(3) + line))
        self._redraw()

    def _draw(self):
        self.delete("all")
        w, h = self._wh()
        round_rect(self, 0, 0, w - 1, h - 1, theme.px(4), fill=theme.FIELD,
                   outline=theme.LINE, width=max(1, theme.px(1)))
        self.create_text(w // 2, h // 2, text=self._text, font=theme.font(13),
                         fill=theme.TEXT)


class Divider(tk.Frame):
    """A 1-px CARD_LINE rule."""

    def __init__(self, master, **kw):
        super().__init__(master, height=max(1, theme.px(1)), bg=theme.CARD_LINE, bd=0,
                         highlightthickness=0, **kw)


class Progress(_Drawn):
    """A progress bar; set(frac), or set(None) when the total is unknown
    (the track shows empty)."""

    def __init__(self, master, *, height=6, **kw):
        self._frac = None
        super().__init__(master, theme.px(200), theme.px(height), **kw)
        self._redraw()

    def set(self, frac):
        if frac is not None:
            frac = min(1.0, max(0.0, frac))
        if frac != self._frac:
            self._frac = frac
            self._redraw()

    def _draw(self):
        self.delete("all")
        w, h = self._wh()
        round_rect(self, 0, 0, w - 1, h - 1, h / 2, fill=theme.LINE, outline=theme.LINE)
        if self._frac:
            x = max(h - 1, round((w - 1) * self._frac))
            round_rect(self, 0, 0, x, h - 1, h / 2, fill=theme.ACCENT, outline=theme.ACCENT)


class RichText(tk.Text):
    """Read-only text with bold runs: parts = [(text, "bold" or None), ...].
    It wraps at the width pack gives it and grows to the lines it needs;
    `width` (design px) is only what it asks for."""

    def __init__(self, master, parts, *, width=None, font=None, **kw):
        font = font or theme.font(14)
        bg = kw.pop("bg", _bg_of(master))
        super().__init__(master, wrap="word", height=1, width=20, relief="flat", bd=0,
                         highlightthickness=0, takefocus=0, cursor="", fg=theme.TEXT,
                         bg=bg, selectbackground=bg, selectforeground=theme.TEXT,
                         inactiveselectbackground=bg, insertwidth=0, padx=0, pady=0,
                         exportselection=False, font=font, **kw)
        if width:
            zero = max(1, int(self.tk.call("font", "measure", font, "-displayof", self._w, "0")))
            self.configure(width=max(10, theme.px(width) // zero))
        bold = tuple(font[:2]) + ("bold",) if isinstance(font, tuple) else font
        self.tag_configure("bold", font=bold)
        for text, style in parts:
            self.insert("end", text, (style,) if style else ())
        self.configure(state="disabled")
        self.bind("<Configure>", lambda e: self._fit_height(), add="+")

    def _fit_height(self):
        try:
            lines = int(self.tk.call(self._w, "count", "-displaylines", "1.0", "end-1c")) + 1
            if lines != int(self.cget("height")):
                self.configure(height=lines)
        except (tk.TclError, ValueError):
            pass                 # destroyed meanwhile
