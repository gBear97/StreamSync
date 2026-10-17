"""Checks the app-drawn controls in ui/widgets.py and the icons in ui/icons.py.

Real Tk on a real (small) window: buttons defer their commands and keep a
state of their own, disabled controls leave the Tab order, controls bound
to a variable repaint when code writes it and let go of it when destroyed,
sizes scale with the theme, and the timeline previews while dragging and
seeks once, after the release. Prints SKIPPED where there is no display
(the ubuntu CI runner).

With STREAMSYNC_SCREENSHOTS set to a folder, a screenshot of every control
is saved there as widgets-gallery-win.png or widgets-gallery-mac.png (best
effort: a failed capture prints SKIPPED and fails nothing).

    python test_widgets.py             # the checks
    python test_widgets.py --gallery   # the checks, then the gallery stays open

No config, no player and no app: nothing here can touch ~/.streamsync.json.
"""

import os
import subprocess
import sys
import time

from ui import icons, theme, widgets

fails = []


def check(label, got, want=True):
    if got != want:
        fails.append(f"{label}: got {got!r}, wanted {want!r}")


def pump(root, seconds=0.05):
    end = time.monotonic() + seconds
    while True:
        root.update()
        if time.monotonic() >= end:
            break
        time.sleep(0.01)


def texts(canvas):
    return [canvas.itemcget(i, "text") for i in canvas.find_all()
            if canvas.type(i) == "text"]


def fills(canvas):
    return {canvas.itemcget(i, "fill") for i in canvas.find_all()
            if canvas.type(i) in ("polygon", "oval", "rectangle", "line")}


def click(widget, x=None, y=None):
    """A real press and release over the widget."""
    x = widget.winfo_width() // 2 if x is None else x
    y = widget.winfo_height() // 2 if y is None else y
    widget.event_generate("<Enter>", x=x, y=y)
    widget.event_generate("<ButtonPress-1>", x=x, y=y, button=1)
    widget.event_generate("<ButtonRelease-1>", x=x, y=y, button=1)
    widget.event_generate("<Leave>", x=x, y=y)


def canvas_state(widget):
    """The Canvas's own -state, behind the control's."""
    return str(widget.tk.call(widget._w, "cget", "-state"))


# ----------------------------------------------------------------- no display

def no_display_checks():
    check("fmt_clock m:ss", widgets.fmt_clock(65.9), "1:05")
    check("fmt_clock h:mm:ss", widgets.fmt_clock(4360), "1:12:40")
    check("fmt_clock zero", widgets.fmt_clock(0), "0:00")
    check("icon names", set(icons.NAMES), {
        "film", "resync", "fullscreen", "speaker_muted", "speaker_on", "subtitles",
        "settings", "people", "pause", "play", "stop", "frame_back", "frame_fwd",
        "download", "chevron"})
    check("theme scale starts at 1", theme.px(36), 36)


# ---------------------------------------------------------------- with Tk

def buttons(root, top):
    tk = widgets.tk
    calls = []
    b = widgets.FlatButton(top, "Go", command=lambda: calls.append("go"))
    b.pack(pady=4)
    pump(root)
    b.invoke()
    check("invoke defers the command", calls, [])
    pump(root)
    check("invoke runs it on the next pass", calls, ["go"])
    click(b)
    check("a click defers too", calls, ["go"])
    pump(root)
    check("a click runs it once", calls, ["go", "go"])

    b.config(state="disabled")
    check("cget state", b.cget("state"), "disabled")
    check("item access", b["state"], "disabled")
    check("the Canvas's own -state stays normal", canvas_state(b), "normal")
    check("disabled leaves the Tab order", str(b.tk.call(b._w, "cget", "-takefocus")), "0")
    check("disabled shows the plain cursor", b.tk.call(b._w, "cget", "-cursor"), "")
    b.invoke()
    click(b)
    pump(root)
    check("a disabled button does nothing", calls, ["go", "go"])
    check("disabled draws the disabled colours",
          theme.DISABLED_FG in fills(b) | {b.itemcget(i, "fill") for i in b.find_all()}, True)
    b.config(state="normal")
    check("enabling restores takefocus", str(b.tk.call(b._w, "cget", "-takefocus")), "1")
    check("enabling restores the cursor", b.tk.call(b._w, "cget", "-cursor"), "hand2")

    b.config(text="Longer text here", kind="primary")
    pump(root)
    check("config text", b.cget("text"), "Longer text here")
    check("config kind", b.cget("kind"), "primary")
    check("the text is drawn", texts(b), ["Longer text here"])
    check("primary draws the accent", theme.ACCENT in fills(b), True)
    wide = b.winfo_reqwidth()
    b.config(text="Go")
    check("the width follows the text", b.winfo_reqwidth() < wide, True)
    try:
        b.config(kind="fancy")
        check("an unknown kind raises", False)
    except ValueError:
        pass
    try:
        widgets.FlatButton(top, "x", kind="fancy")
        check("an unknown kind raises at construction", False)
    except ValueError:
        pass
    check("Canvas options still pass through", (b.config(bg="#000000"), b.cget("bg"))[1],
          "#000000")
    b.configure(command=lambda: calls.append("new"))
    b.invoke()
    pump(root)
    check("config command", calls[-1], "new")
    check("Return is bound", "<Key-Return>" in b.bind(), True)
    check("Space is not bound (the pause key)", "<Key-space>" in b.bind(), False)
    b._key()                     # what the Return binding runs
    pump(root)
    check("Return invokes", calls[-1:] + [len(calls)], ["new", 4])

    # the focus ring: drawn on <FocusIn>, which needs the OS's focus, so
    # the handler is called directly
    plain = len(b.find_all())
    b._set_focus(True)
    check("focus draws a ring", len(b.find_all()) > plain, True)
    b._set_focus(False)
    check("...and losing it removes the ring", len(b.find_all()), plain)
    links(root, top)

    # the outline option and the other kinds draw without error
    for kind in ("secondary", "ghost", "link", "round", "bar", "chip"):
        k = widgets.FlatButton(top, "" if kind == "round" else kind, kind=kind,
                               icon="play" if kind == "round" else "resync")
        k.pack(side="left")
        check(f"{kind} draws", len(k.find_all()) > 1, True)
    g = widgets.FlatButton(top, "Film sound on", kind="ghost", icon="speaker_on",
                           outline=theme.ACCENT)
    g.pack(side="left")
    check("outline draws round the button", any(
        g.type(i) == "arc" and g.itemcget(i, "outline") == theme.ACCENT
        for i in g.find_all()), True)
    check("cget outline", g.cget("outline"), theme.ACCENT)

    # Tab skips a disabled button: the FirstRun Sync button
    row = tk.Frame(top, bg=theme.GROUND)
    row.pack()
    one = widgets.FlatButton(row, "One")
    two = widgets.FlatButton(row, "Two", state="disabled")
    three = widgets.FlatButton(row, "Three")
    for w in (one, two, three):
        w.pack(side="left")
    pump(root, 0.1)
    check("Tab skips the disabled button", str(one.tk_focusNext()), str(three))
    two.config(state="normal")
    check("Tab reaches it once enabled", str(one.tk_focusNext()), str(two))
    nofocus = widgets.FlatButton(row, "Bar", kind="bar", takefocus=False)
    nofocus.pack(side="left")
    nofocus.config(state="disabled")
    nofocus.config(state="normal")
    check("takefocus False stays out of the Tab order",
          str(nofocus.tk.call(nofocus._w, "cget", "-takefocus")), "0")

    # tab_last: a button made first so that pack keeps it at the row's right
    # end is reached last, where it sits (through Tk: a Canvas's own lift
    # raises canvas items)
    row = tk.Frame(top, bg=theme.GROUND)
    row.pack(fill="x")
    end = widgets.FlatButton(row, "End")
    end.pack(side="right")
    start = widgets.FlatButton(row, "Start")
    start.pack(side="left")
    pump(root, 0.1)
    check("Tab follows the making order, not the layout", str(end.tk_focusNext()), str(start))
    where = (start.winfo_x(), end.winfo_x())
    widgets.tab_last(end)
    pump(root)
    check("tab_last: the left button, then the right one", str(start.tk_focusNext()), str(end))
    check("tab_last: the stacking order is the visual one",
          [str(w) for w in row.winfo_children()], [str(start), str(end)])
    check("tab_last moves nothing on screen", (start.winfo_x(), end.winfo_x()), where)


def links(root, top):
    """A link sitting in its sentence (padx 0) has no room beside its text
    for a focus ring, which was drawn over its first and last letters: its
    underline thickens instead, below the baseline. A link with padding
    keeps the ring."""
    tk = widgets.tk
    f = theme.font(13)
    row = tk.Frame(top, bg=theme.GROUND)
    row.pack(anchor="w")
    tk.Label(row, text="Listening to: Speakers ·", font=f, bg=theme.GROUND,
             fg=theme.MUTED).pack(side="left")
    link = widgets.FlatButton(row, "Show film window", kind="link", height=18, font=f, padx=0,
                              underline=True)
    link.pack(side="left")
    pump(root)

    def items(kind):
        return [i for i in link.find_all() if link.type(i) == kind]

    (text,) = items("text")
    check("an inline link is underlined", "underline" in link.itemcget(text, "font"), True)
    check("an inline link takes the link colour", link.itemcget(text, "fill"), theme.LINK)
    x0 = link.coords(text)[0]
    link._set_focus(True)
    (text,) = items("text")
    check("a focused inline link draws no ring over its letters",
          [link.type(i) for i in link.find_all() if link.type(i) in ("arc", "line", "oval")], [])
    check("...its text brightens", link.itemcget(text, "fill"), theme.LINK_HOVER)
    marks = [link.coords(i) for i in items("rectangle")]
    check("...and one thick underline marks it", len(marks), 1)
    if len(marks) == 1:
        mx0, my0, mx1, my1 = marks[0]
        top_y = link.bbox(text)[1]
        ascent = int(link.tk.call("font", "metrics", f, "-ascent"))
        width = int(link.tk.call("font", "measure", f, "Show film window"))
        check("...as wide as the text", (mx0, mx1 - mx0), (x0, width))
        check("...as thick as a ring", my1 - my0, max(2, theme.px(2)))
        check("...below the baseline, clear of the letters", my0 >= top_y + ascent, True)
        check("...inside the link", my1 <= link.winfo_height(), True)
    check("...in place of the font's thin one", "underline" in link.itemcget(text, "font"),
          False)
    link._set_focus(False)
    (text,) = items("text")
    check("losing the focus brings the thin underline back",
          ("underline" in link.itemcget(text, "font"), items("rectangle")), (True, []))
    padded = widgets.FlatButton(row, "Reset", kind="link", height=30, padx=8)
    padded.pack(side="left")
    pump(root)
    padded._set_focus(True)
    check("a link with room beside its text keeps the ring",
          any(padded.type(i) == "arc" for i in padded.find_all()), True)


def variable_controls(root, top):
    tk = widgets.tk
    on = tk.BooleanVar(top, value=False)
    ran = []
    s = widgets.Switch(top, on, command=lambda: ran.append(on.get()))
    s.pack(pady=4)
    pump(root)

    def knob_x(sw):
        # the knob is drawn last, over the track's round ends
        ovals = [sw.coords(i) for i in sw.find_all() if sw.type(i) == "oval"]
        return ovals[-1][0]

    off_x = knob_x(s)
    on.set(True)                 # a write from code: _on_auto_off's auto_var.set(False)
    check("Switch repaints on var.set", knob_x(s) > off_x, True)
    on.set(False)
    check("Switch repaints back", knob_x(s), off_x)
    click(s)
    check("a Switch click sets the variable at once", on.get(), True)
    check("...and defers the command", ran, [])
    pump(root)
    check("...which runs on the next pass", ran, [True])
    s.config(state="disabled")
    click(s)
    pump(root)
    check("a disabled Switch ignores clicks", (on.get(), ran), (True, [True]))
    check("a disabled Switch keeps the Canvas -state", canvas_state(s), "normal")

    follow = tk.BooleanVar(top, value=False)
    c = widgets.Check(top, "Follow the stream's pauses", follow)
    c.pack(anchor="w")
    pump(root)
    check("Check draws its label", texts(c), ["Follow the stream's pauses"])
    check("Check off has no accent", theme.ACCENT in fills(c), False)
    follow.set(True)
    check("Check repaints on var.set", theme.ACCENT in fills(c), True)
    c.invoke()
    check("Check invoke flips the variable", follow.get(), False)

    player = tk.StringVar(top, value="embedded")
    picked = []
    r1 = widgets.Radio(top, "Built-in player", player, "embedded",
                       sub="The most precise sync.", command=lambda: picked.append(player.get()))
    r2 = widgets.Radio(top, "VLC app", player, "external",
                       command=lambda: picked.append(player.get()))
    r1.pack(anchor="w")
    r2.pack(anchor="w")
    pump(root)
    check("Radio draws its label and sub-label", texts(r1),
          ["Built-in player", "The most precise sync."])
    check("the chosen Radio shows", (theme.ACCENT in fills(r1), theme.ACCENT in fills(r2)),
          (True, False))
    player.set("external")       # a write from code: a failed switch resets it
    check("Radios repaint on var.set",
          (theme.ACCENT in fills(r1), theme.ACCENT in fills(r2)), (False, True))
    click(r1)
    pump(root)
    check("a Radio click picks its value", (player.get(), picked), ("embedded", ["embedded"]))
    click(r1)
    pump(root)
    check("a click on the choice already made runs nothing", picked, ["embedded"])

    dev = tk.StringVar(top, value="a")
    chosen = []
    sel = widgets.Select(top, dev, values=("a", "b"), label_for=lambda v: f"Device {v.upper()}",
                         command=lambda: chosen.append(dev.get()))
    sel.pack(fill="x")
    pump(root)
    check("Select shows label_for(value)", texts(sel), ["Device A"])
    dev.set("b")
    check("Select repaints on var.set", texts(sel), ["Device B"])
    sel.choose("a")
    check("choose sets the variable", dev.get(), "a")
    check("choose defers the command", chosen, [])
    pump(root)
    check("...which runs on the next pass", chosen, ["a"])
    sel.set_values(["a", "b", "c"])
    check("set_values", sel._values, ["a", "b", "c"])
    sel.config(state="disabled")
    check("Select takes a state", (sel.cget("state"), canvas_state(sel)), ("disabled", "normal"))
    check("a disabled Select leaves the Tab order",
          str(sel.tk.call(sel._w, "cget", "-takefocus")), "0")
    sel.config(state="normal")
    long = widgets.Select(top, tk.StringVar(top, value="x" * 300), width=120)
    long.pack()
    pump(root)
    check("a long label is cut to fit", texts(long)[0].endswith("…"), True)

    # a destroyed widget's trace is gone
    v = tk.BooleanVar(top)
    sv = tk.StringVar(top)
    made = [widgets.Switch(top, v), widgets.Check(top, "x", v),
            widgets.Radio(top, "y", sv, "y"), widgets.Select(top, sv),
            widgets.Field(top, textvariable=sv, placeholder="p")]
    check("the controls trace their variables", (len(v.trace_info()), len(sv.trace_info())),
          (2, 3))
    for w in made:
        w.destroy()
    pump(root)
    check("destroying them removes every trace", (v.trace_info(), sv.trace_info()), ([], []))
    v.set(True)                  # and a write afterwards raises nothing
    sv.set("z")


def timeline(root, top):
    seeks, previews = [], []
    allowed = [False]
    t = widgets.Timeline(top, width=216, on_seek=seeks.append, on_preview=previews.append,
                         can_seek=lambda: allowed[0])
    t.pack(fill="x", padx=10)
    pump(root, 0.1)
    w = t.winfo_width()
    check("Timeline: highlightthickness 0", int(t.cget("highlightthickness")), 0)
    check("Timeline: takefocus 0", str(t.cget("takefocus")), "0")
    # The default inset is 8 design px: 8 screen px at 100%, 12 at 150% and
    # 16 at 200%, where main() runs this on a Windows display set that way.
    i = t.inset
    check("Timeline: the default inset is 8 design px", i, theme.px(8))
    for x in (-5, 0, i, 50, w // 2, w - i, w + 30):
        # the 1.x fullscreen bar's math: `inset` px in from either end
        want = min(1.0, max(0.0, (x - i) / max(1, w - 2 * i)))
        check(f"frac_at({x})", round(t.frac_at(x), 9), round(want, 9))
    t.set(0.25)
    check("set draws the knob", len([i for i in t.find_all() if t.type(i) == "oval"]), 1)
    t.set(None)
    check("set(None) hides the knob", [i for i in t.find_all() if t.type(i) == "oval"], [])

    t.event_generate("<ButtonPress-1>", x=60, y=10, button=1)
    check("a press is ignored when can_seek is False", (t.dragging, previews), (False, []))
    t.event_generate("<ButtonRelease-1>", x=60, y=10, button=1)
    pump(root)
    check("...and so is its release", seeks, [])

    allowed[0] = True
    t.event_generate("<ButtonPress-1>", x=40, y=10, button=1)
    check("a press starts a drag", t.dragging, True)
    t.event_generate("<B1-Motion>", x=80, y=10, state=0x100)
    check("a drag previews", [round(p, 6) for p in previews],
          [round(t.frac_at(40), 6), round(t.frac_at(80), 6)])
    t.set(0.9)
    check("set is ignored while dragging",
          [round(t.coords(i)[0] + t._knob_r) for i in t.find_all() if t.type(i) == "oval"],
          [round(t.inset + (w - 2 * t.inset) * t.frac_at(80))])
    t.event_generate("<ButtonRelease-1>", x=100, y=10, button=1)
    check("the release does not seek inside the handler", seeks, [])
    pump(root)
    check("the release seeks once, on the next pass", [round(s, 6) for s in seeks],
          [round(t.frac_at(100), 6)])
    pump(root)
    check("...and only once", len(seeks), 1)
    t.event_generate("<ButtonPress-1>", x=40, y=10, button=1)
    t.cancel_drag()
    t.event_generate("<ButtonRelease-1>", x=40, y=10, button=1)
    pump(root)
    check("cancel_drag drops the seek", len(seeks), 1)


def scrollbar(root, top):
    """The drawn scrollbar (Settings' scroll area): Tk's scrollbar
    protocol, a thumb in the theme's colours, its clicks and drags run on
    the next pass, and never a Tab stop."""
    tk = widgets.tk
    holder = tk.Frame(top, bg=theme.GROUND)
    holder.pack()
    before = widgets.FlatButton(holder, "Before")
    before.pack()
    box = tk.Frame(holder, bg=theme.GROUND)
    box.pack()
    view = tk.Canvas(box, width=200, height=120, bg=theme.GROUND, highlightthickness=0,
                     yscrollincrement=10, scrollregion=(0, 0, 200, 600))
    sb = widgets.Scrollbar(box, command=view.yview)
    view.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    view.pack(side="left")
    after = widgets.FlatButton(holder, "After")
    after.pack()
    pump(root, 0.1)

    def first():
        return round(view.yview()[0], 4)

    check("Scrollbar: never a Tab stop", str(before.tk_focusNext()), str(after))
    check("Scrollbar: takefocus 0", str(sb.cget("takefocus")), "0")
    check("Scrollbar: set() follows the view", [round(f, 4) for f in sb.get()],
          [round(f, 4) for f in view.yview()])
    check("Scrollbar: the thumb is drawn in LINE", fills(sb), {theme.LINE})
    top_y, bottom_y, travel = sb._thumb()
    check("Scrollbar: the thumb starts at the top of its track", top_y, theme.px(2))
    check("Scrollbar: and shows the part in view", travel > 0 and bottom_y > top_y, True)
    sb.event_generate("<Enter>", x=2, y=2)
    check("Scrollbar: brighter under the pointer", fills(sb), {theme.FAINT})
    sb.event_generate("<Leave>", x=2, y=2)

    sb.event_generate("<ButtonPress-1>", x=sb.winfo_width() // 2, y=bottom_y + 20, button=1)
    check("Scrollbar: a click on the track waits for the next pass", first(), 0.0)
    sb.event_generate("<ButtonRelease-1>", x=sb.winfo_width() // 2, y=bottom_y + 20, button=1)
    pump(root)
    paged = first()
    check("Scrollbar: then scrolls a page down", 0.1 < paged < 0.25, True)
    check("Scrollbar: and the thumb follows", sb._thumb()[0] > top_y, True)

    top_y, bottom_y, travel = sb._thumb()
    x, y = sb.winfo_width() // 2, (top_y + bottom_y) // 2
    sb.event_generate("<ButtonPress-1>", x=x, y=y, button=1)
    sb.event_generate("<Leave>", x=x, y=y)
    check("Scrollbar: a held thumb stays bright", fills(sb), {theme.FAINT})
    sb.event_generate("<B1-Motion>", x=x, y=y + travel // 2, state=0x100)
    check("Scrollbar: a drag waits for the next pass", first(), paged)
    pump(root)
    want = paged + (travel // 2) / travel * 0.8
    check("Scrollbar: then moves the view with the thumb", abs(first() - want) < 0.02, True)
    sb.event_generate("<ButtonRelease-1>", x=x, y=y + travel // 2, button=1)
    check("Scrollbar: let go, the thumb dims", fills(sb), {theme.LINE})
    sb.set(0.0, 1.0)
    check("Scrollbar: nothing is drawn when all of it shows", sb.find_all(), ())


def fields(root, top):
    tk = widgets.tk
    f = widgets.Field(top, placeholder="h:mm:ss", width=120)
    f.pack(pady=4)
    pump(root, 0.1)
    check("Field: the placeholder shows over an empty field",
          bool(f._placeholder.winfo_ismapped()), True)
    check("Field: placeholder text", f._placeholder.cget("text"), "h:mm:ss")
    f.set("1:02:03")
    pump(root)
    check("Field: set and get", f.get(), "1:02:03")
    check("Field: the placeholder hides once there is text",
          bool(f._placeholder.winfo_ismapped()), False)
    f.set("")
    pump(root)
    check("Field: and comes back when it is cleared", bool(f._placeholder.winfo_ismapped()), True)
    f.set_error(True)
    check("Field: set_error shows the red border", f.cget("highlightbackground"), theme.ERROR)
    check("Field: .entry reports it too", f.entry.cget("highlightbackground"), theme.ERROR)
    f.set_error(False)
    check("Field: and clears it", f.cget("highlightbackground"), theme.LINE)
    check("Field: .entry is a tk.Entry", f.entry.winfo_class(), "Entry")
    f._focus(True)
    check("Field: the border turns accent with the keyboard", f.cget("highlightbackground"),
          theme.ACCENT)
    f._focus(False)
    pw = widgets.Field(top, show="•")
    pw.pack()
    check("Field: show", pw.entry.cget("show"), "•")
    shared = tk.StringVar(top, value="ws://relay")
    rel = widgets.Field(top, textvariable=shared, height=32, font=theme.font(13))
    rel.pack(fill="x")
    check("Field: textvariable", rel.get(), "ws://relay")


def the_rest(root, top):
    card = widgets.Card(top)
    card.pack(fill="x", pady=4)
    d = widgets.Dot(card.body, theme.SYNCED)
    d.pack(side="left")
    d.set(theme.ERROR)
    check("Dot.set recolours", fills(d), {theme.ERROR})
    tile = []
    it = widgets.IconTile(card.body, "film", 44, radius=8, fg=theme.MUTED,
                          command=lambda: tile.append(1))
    it.pack(side="left")
    k = widgets.Kbd(card.body, "Space")
    k.pack(side="left")
    pump(root)
    check("Kbd is at least 54 wide", k.winfo_reqwidth() >= 54, True)
    check("Kbd shows its key", texts(k), ["Space"])
    click(it)
    check("IconTile defers its command", tile, [])
    pump(root)
    check("...and runs it", tile, [1])
    widgets.Divider(card.body).pack(fill="x")
    p = widgets.Progress(card.body)
    p.pack(fill="x")
    p.set(0.5)
    check("Progress fills with the accent", theme.ACCENT in fills(p), True)
    p.set(None)
    check("Progress set(None) shows the empty track", theme.ACCENT in fills(p), False)
    rt = widgets.RichText(card.body, [("Press ", None), ("Sync to stream", "bold"),
                                      (". StreamSync listens.", None)], width=200)
    rt.pack(fill="x")
    pump(root, 0.1)
    check("RichText holds the text", rt.get("1.0", "end-1c"),
          "Press Sync to stream. StreamSync listens.")
    check("RichText bold run", rt.tag_ranges("bold") != (), True)
    check("RichText is read-only and out of the Tab order",
          (str(rt.cget("state")), str(rt.cget("takefocus"))), ("disabled", "0"))
    check("RichText grows to its lines", int(rt.cget("height")) >= 1, True)
    check("ellipsize cuts", widgets.ellipsize("x" * 200, theme.font(14), 80).endswith("…"), True)
    check("ellipsize keeps what fits", widgets.ellipsize("Film", theme.font(14), 500), "Film")

    for name in icons.NAMES:
        items = icons.draw(it, name, 0, 0, 24, theme.TEXT)
        check(f"icon {name} draws", len(items) > 0, True)
        for i in items:
            it.delete(i)
    try:
        icons.draw(it, "nope", 0, 0, 24, theme.TEXT)
        check("an unknown icon raises", False)
    except ValueError:
        pass

    drawn = [w for w in walk(top) if isinstance(w, widgets.tk.Canvas)]
    check("every drawn widget has highlightthickness 0",
          [str(w) for w in drawn if int(w.cget("highlightthickness")) != 0], [])
    check("...and bd 0", [str(w) for w in drawn if int(w.cget("bd")) != 0], [])


def walk(w):
    for c in w.winfo_children():
        yield c
        yield from walk(c)


def scaled(root):
    """Sizes are design px: each control scales its own (design rule 3)."""
    tk = widgets.tk
    was = (theme.SCALE, theme.FAMILY)
    theme.init(1.5, theme.FAMILY)
    try:
        top = tk.Toplevel(root, bg=theme.GROUND)
        b = widgets.FlatButton(top, "Scaled", height=36)
        t = widgets.Timeline(top)
        s = widgets.Switch(top, tk.BooleanVar(top))
        sel = widgets.Select(top, tk.StringVar(top), height=36, width=200)
        f = widgets.Field(top, width=72, height=36)
        it = widgets.IconTile(top, "download", 44)
        for w in (b, t, s, sel, f, it):
            w.pack()
        pump(root)
        check("1.5: FlatButton(height=36) is 54 high", b.winfo_reqheight(), 54)
        check("1.5: Timeline().inset is 12", t.inset, 12)
        check("1.5: Switch is 60x33", (s.winfo_reqwidth(), s.winfo_reqheight()), (60, 33))
        check("1.5: Select(width=200, height=36)", (sel.winfo_reqwidth(), sel.winfo_reqheight()),
              (300, 54))
        check("1.5: Field(width=72, height=36)", (f.winfo_reqwidth(), f.winfo_reqheight()),
              (108, 54))
        check("1.5: IconTile(44)", it.winfo_reqwidth(), 66)
        top.destroy()
    finally:
        theme.init(*was)


# ------------------------------------------------------------------ gallery

def gallery(root):
    tk = widgets.tk
    top = tk.Toplevel(root, bg=theme.GROUND)
    top.title("StreamSync - widget gallery")
    body = tk.Frame(top, bg=theme.GROUND)
    body.pack(padx=theme.px(22), pady=theme.px(20))
    for state in ("normal", "disabled"):
        row = tk.Frame(body, bg=theme.GROUND)
        row.pack(anchor="w", pady=theme.px(4))
        widgets.FlatButton(row, "Sync to stream", kind="primary", height=48,
                           font=theme.font(16, True), state=state).pack(side="left")
        widgets.FlatButton(row, "Resync", icon="resync", height=48, font=theme.font(15),
                           padx=18, state=state).pack(side="left", padx=theme.px(10))
        widgets.FlatButton(row, "Settings", icon="settings", kind="ghost",
                           state=state).pack(side="left")
        widgets.FlatButton(row, "change", kind="link", font=theme.font(13),
                           state=state).pack(side="left", padx=theme.px(8))
    card = widgets.Card(body)
    card.pack(fill="x", pady=theme.px(8))
    row = tk.Frame(card.body, bg=theme.CARD)
    row.pack(anchor="w")
    widgets.FlatButton(row, kind="round", icon="pause", height=44).pack(side="left")
    widgets.FlatButton(row, "Fullscreen", icon="fullscreen").pack(side="left", padx=theme.px(8))
    widgets.FlatButton(row, "Film muted", icon="speaker_muted").pack(side="left")
    widgets.FlatButton(row, "Film sound on", icon="speaker_on", kind="ghost",
                       outline=theme.ACCENT).pack(side="left", padx=theme.px(8))
    row = tk.Frame(card.body, bg=theme.CARD)
    row.pack(fill="x", pady=theme.px(10))
    widgets.Dot(row, theme.SYNCED).pack(side="left")
    tk.Label(row, text="Synced - stream at 1:12:40", bg=theme.CARD, fg=theme.TEXT,
             font=theme.font(14)).pack(side="left", padx=theme.px(10))
    for d in ("−0.5", "−0.1", "+0.1", "+0.5"):
        widgets.FlatButton(row, d, kind="chip", height=30).pack(side="left", padx=theme.px(3))
    tl = widgets.Timeline(card.body, rest=theme.LINE, knob_r=8, thickness=6)
    tl.pack(fill="x")
    tl.set(0.43)
    row = tk.Frame(card.body, bg=theme.CARD)
    row.pack(anchor="w", pady=theme.px(10))
    widgets.Switch(row, tk.BooleanVar(row, value=True)).pack(side="left")
    widgets.Switch(row, tk.BooleanVar(row, value=False)).pack(side="left", padx=theme.px(10))
    widgets.Check(row, "Follow", tk.BooleanVar(row, value=True)).pack(side="left")
    widgets.Check(row, "Off", tk.BooleanVar(row)).pack(side="left", padx=theme.px(10))
    v = tk.StringVar(row, value="a")
    widgets.Radio(row, "Built-in player", v, "a", sub="The most precise sync.").pack(side="left")
    widgets.Radio(row, "VLC app", v, "b").pack(side="left", padx=theme.px(10))
    row = tk.Frame(body, bg=theme.GROUND)
    row.pack(fill="x", pady=theme.px(4))
    widgets.Select(row, tk.StringVar(row, value="Speakers (system default)"),
                   width=240).pack(side="left")
    widgets.Field(row, placeholder="h:mm:ss", width=140).pack(side="left", padx=theme.px(10))
    bad = widgets.Field(row, width=72)
    bad.set("abc")
    bad.set_error(True)
    bad.pack(side="left")
    row = tk.Frame(body, bg=theme.GROUND)
    row.pack(fill="x", pady=theme.px(4))
    for k in ("Space", "← →", "F", "Esc"):
        widgets.Kbd(row, k).pack(side="left", padx=(0, theme.px(8)))
    widgets.IconTile(row, "download", 44, fg=theme.ACCENT).pack(side="left")
    widgets.IconTile(row, "film", 64, radius=14, fg=theme.ACCENT).pack(side="left",
                                                                        padx=theme.px(8))
    p = widgets.Progress(row)
    p.pack(side="left", fill="x", expand=True)
    p.set(0.6)
    bar = tk.Frame(body, bg=theme.BAR_BG, highlightthickness=1,
                   highlightbackground=theme.BAR_LINE)
    bar.pack(fill="x", pady=theme.px(8))
    for text, icon, after in (("Pause", "pause", False), ("Stop", "stop", False),
                              ("−1 frame", "frame_back", False), ("+1 frame", "frame_fwd", True),
                              ("Resync here", "resync", False)):
        widgets.FlatButton(bar, text, icon=icon, icon_after=after, kind="bar", height=34,
                           takefocus=False).pack(side="left", padx=(0, theme.px(6)),
                                                 pady=theme.px(10))
    icon_row = tk.Canvas(body, bg=theme.GROUND, highlightthickness=0,
                         width=theme.px(15 * 30), height=theme.px(28))
    icon_row.pack(anchor="w")
    for i, name in enumerate(icons.NAMES):
        icons.draw(icon_row, name, theme.px(i * 30), theme.px(2), theme.px(24), theme.TEXT)
    widgets.RichText(body, [("Press ", None), ("Sync to stream", "bold"),
                            (". StreamSync listens and jumps the film to the same moment.",
                             None)], width=400).pack(fill="x")
    return top


def screenshot(root, top, folder):
    os.makedirs(folder, exist_ok=True)
    top.attributes("-topmost", True)
    top.lift()
    pump(root, 0.6)
    try:
        if sys.platform == "darwin":
            path = os.path.join(folder, "widgets-gallery-mac.png")
            x, y = top.winfo_rootx(), top.winfo_rooty()
            w, h = top.winfo_width(), top.winfo_height()
            r = subprocess.run(["screencapture", "-x", "-R", f"{x},{y},{w},{h}", path],
                               capture_output=True, timeout=20)
            if r.returncode or not os.path.exists(path):
                raise OSError(r.stderr.decode(errors="replace").strip() or "no file")
        elif sys.platform == "win32":
            path = os.path.join(folder, "widgets-gallery-win.png")
            _win_capture(int(top.frame(), 16), path)
        else:
            print("SKIPPED gallery screenshot: no capture on this platform")
            return
        print(f"gallery screenshot: {path}")
    except Exception as e:
        print(f"SKIPPED gallery screenshot: {e}")


def _win_capture(hwnd, path):
    """The window's screen pixels as a PNG, with ctypes and zlib (no Pillow)."""
    import ctypes
    import struct
    import zlib
    from ctypes import wintypes
    u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32
    rect = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    screen = u32.GetDC(0)
    mem = g32.CreateCompatibleDC(screen)
    bmp = g32.CreateCompatibleBitmap(screen, w, h)
    old = g32.SelectObject(mem, bmp)
    try:
        g32.BitBlt(mem, 0, 0, w, h, screen, rect.left, rect.top, 0x00CC0020)  # SRCCOPY

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        info = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        g32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), 0)
    finally:
        g32.SelectObject(mem, old)
        g32.DeleteObject(bmp)
        g32.DeleteDC(mem)
        u32.ReleaseDC(0, screen)
    raw = bytearray()
    data = buf.raw
    for row in range(h):
        raw.append(0)
        line = data[row * w * 4:(row + 1) * w * 4]
        for i in range(0, len(line), 4):
            raw += bytes((line[i + 2], line[i + 1], line[i]))     # BGRA -> RGB

    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
        f.write(chunk(b"IEND", b""))


# --------------------------------------------------------------------- main

def main():
    no_display_checks()
    import tkinter as tk
    if sys.platform == "win32":
        try:                     # as the app does: real pixels at 150% and 200%
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    try:
        root = tk.Tk()
    except tk.TclError as e:
        if fails:
            sys.exit("\n".join(["WIDGETS TEST FAILED"] + fails))
        print(f"SKIPPED: no display ({e})")
        return
    errors = []
    root.report_callback_exception = lambda *a: errors.append(f"{a[0].__name__}: {a[1]}")
    try:
        family = ".AppleSystemUIFont" if sys.platform == "darwin" else (
            "Segoe UI" if sys.platform == "win32" else "TkDefaultFont")
        scale = root.winfo_fpixels("1i") / 96 if sys.platform == "win32" else 1.0
        theme.init(scale, family)
        root.configure(bg=theme.GROUND)
        root.title("StreamSync - widget test")
        root.geometry("+40+40")
        for part in (buttons, variable_controls, timeline, scrollbar, fields, the_rest):
            top = tk.Toplevel(root, bg=theme.GROUND)
            top.title(f"StreamSync - {part.__name__}")
            top.geometry("+60+60")
            pump(root, 0.1)
            part(root, top)
            top.destroy()
        theme.init(1.0, family)
        scaled(root)
        theme.init(scale, family)
        g = gallery(root)
        pump(root, 0.3)
        folder = os.environ.get("STREAMSYNC_SCREENSHOTS")
        if folder:
            screenshot(root, g, folder)
        if "--gallery" in sys.argv:
            g.protocol("WM_DELETE_WINDOW", root.destroy)
            root.withdraw()
            root.mainloop()
            return finish(errors)
        pump(root, 0.1)
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
    finish(errors)


def finish(errors):
    if errors:
        fails.append(f"Tk callback errors: {errors}")
    if fails:
        sys.exit("\n".join(["WIDGETS TEST FAILED"] + fails))
    print("WIDGETS TEST PASSED")


if __name__ == "__main__":
    main()
