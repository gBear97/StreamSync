"""Field probes for the Mac film window - one-offs, not part of the app.

The first probe asked two questions that decide how the next StreamSync
gives the Mac the film window's mouse controls and its fullscreen control
bar, and neither can be answered from a PC:

1. Do Tk's mouse bindings fire over the picture libvlc draws into our
   window (libvlc adds an NSView of its own there) - with libvlc's own
   mouse handling on, and off - and does libvlc act on a double-click by
   itself?
2. Can a small Tk window float over the film while it is fullscreen?
   macOS gives a fullscreen window a Space of its own, so three ways are
   tried: a plain topmost window, the same window marked as a fullscreen
   "auxiliary" that joins every Space (via the Objective-C runtime), and
   no fullscreen Space at all - the film window made borderless and
   stretched over its screen, with the Dock and menu bar hidden.

The second probe settles what the first left open:

A. libvlc's mouse and key input set apart - mouse on and key off, mouse
   off and key on, both off - with clicks, keys and the scroll wheel
   counted app-wide, each with the widget it went to, Tk's focus and
   AppKit's key window, and libvlc's volume and position read around it
   in case libvlc acts on the wheel itself.
B. The wheel's delta values, from a trackpad and from a mouse wheel.
C. The keyboard after clicking a bar built like the Windows one (PR 31)
   over the fullscreen film, and after dragging its timeline.
D. With a second screen: which screen native fullscreen uses, whether the
   bar follows, and where the window goes back to.
E. An NSEvent local scroll-wheel monitor made through ctypes, with a
   hand-built block and no PyObjC - last, because it is the only phase
   with a native callback.

Run either from Terminal against a built app:

    .../StreamSync.app/Contents/MacOS/StreamSync --probe-overlay [film]
    .../StreamSync.app/Contents/MacOS/StreamSync --probe-overlay --first [film]

The first line runs the second probe. Each walks through its phases in a
window of its own and rewrites its log in ~/Library/Logs/StreamSync
(overlay-probe-2.txt, or overlay-probe.txt for the first) after every
phase, so even a crash leaves the answers so far. Neither reads or writes
the app's settings, and the film is named on the log's "film:" line only.
"""

import ctypes
import ctypes.util
import os
import platform
import subprocess
import sys
import time
import tkinter as tk
import traceback
from collections import Counter, namedtuple
from tkinter import filedialog

import diagnostics
from players import EmbeddedPlayer, _tk_nsview, vlc
from version import __version__

IS_MAC = sys.platform == "darwin"
OUT = os.path.join(diagnostics.LOG_DIR, "overlay-probe.txt")
OUT2 = os.path.join(diagnostics.LOG_DIR, "overlay-probe-2.txt")
ORANGE, INK = "#ff8a1f", "#17181a"
BAR_BG, BAR_FG, BAR_KEY = "#1b1b1b", "#e6e6e6", "#2c2c2c"   # PR 31's bar
BAR_WAIT = 20.0            # seconds a fullscreen phase waits for an answer
BAR_KEYS_WAIT = 90.0       # ...and phase C, which asks for more
PHASES = 5

# What the film window listens for. On Aqua Tk 8.6 a right-click arrives
# as Button-2 (and as Control-Button-1 from a one-button mouse); Button-3
# is listed in case this Tk numbers buttons the other way.
EVENTS = (("<Button-1>", "click"), ("<Double-Button-1>", "double"),
          ("<Button-2>", "button2"), ("<Button-3>", "button3"),
          ("<Control-Button-1>", "ctrl_click"), ("<MouseWheel>", "wheel"),
          ("<Motion>", "motion"))

# What the second probe counts app-wide (bind_all), wherever Tk delivers
# it. A Control-click, and a Shift-wheel (sideways scrolling on Aqua),
# are told apart by their modifier bits.
APP_EVENTS = (("<Button-1>", "click"), ("<Double-Button-1>", "double"),
              ("<Button-2>", "button2"), ("<Button-3>", "button3"),
              ("<MouseWheel>", "wheel"), ("<KeyPress>", "key"))
MOUSE_KINDS = ("click", "double", "button2", "button3", "ctrl_click",
               "wheel", "hwheel")
SHIFT, CONTROL = 0x1, 0x4

# One app-wide event: when, what, the widget it went to, Tk's focus and
# AppKit's key window at that moment, the wheel delta, the key, and the
# step of the phase it came in.
Seen = namedtuple("Seen", "t kind target focus keywin delta keysym stage")

# The Blocks ABI, just enough for one global block: no captured
# variables, so no copy or dispose helpers, and the runtime never copies
# or frees it - the layout clang emits for a block at file scope.
BLOCK_IS_GLOBAL = 1 << 28
BLOCK_HAS_SIGNATURE = 1 << 30
NS_EVENT_MASK_SCROLL_WHEEL = 1 << 22

# NSEvent *(^)(NSEvent *): a block's function takes the block first
EVENT_HANDLER = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_void_p)
_KEEP = []   # every wheel monitor made: kept alive for the whole run


class NSRect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double),
                ("w", ctypes.c_double), ("h", ctypes.c_double)]


class BlockDescriptor(ctypes.Structure):
    _fields_ = [("reserved", ctypes.c_ulong), ("size", ctypes.c_ulong),
                ("signature", ctypes.c_char_p)]


class Block(ctypes.Structure):
    _fields_ = [("isa", ctypes.c_void_p), ("flags", ctypes.c_int),
                ("reserved", ctypes.c_int), ("invoke", ctypes.c_void_p),
                ("descriptor", ctypes.POINTER(BlockDescriptor))]


def global_block(invoke, isa, signature):
    """A global block literal around a ctypes callback; isa is the
    address of _NSConcreteGlobalBlock. The caller keeps the callback and
    the result alive for as long as anything may call the block."""
    block = Block()
    block.isa = isa
    block.flags = BLOCK_IS_GLOBAL | BLOCK_HAS_SIGNATURE
    block.reserved = 0
    block.invoke = ctypes.cast(invoke, ctypes.c_void_p).value
    block.descriptor = ctypes.pointer(
        BlockDescriptor(0, ctypes.sizeof(Block), signature))
    return block


def tally(items):
    """'a 3, b 1', most first."""
    counts = Counter(items).most_common()
    return ", ".join(f"{k} {n}" for k, n in counts) or "-"


def _num(v):
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return str(v)


def stats(values):
    if not values:
        return "none"
    return (f"count {len(values)}, min {_num(min(values))}, "
            f"max {_num(max(values))}, sum {_num(sum(values))}")


def clock(ms):
    if ms is None or ms < 0:
        return "-:--"
    s = int(ms // 1000)
    if s >= 3600:
        return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


class ObjC:
    """Just enough of the Objective-C runtime to read and set a few
    NSWindow and NSApplication properties. Every message gets an exact
    prototype: on Apple Silicon objc_msgSend must never be called as a
    variadic function."""

    def __init__(self):
        self.lib = ctypes.CDLL(ctypes.util.find_library("objc"))
        self.lib.sel_registerName.restype = ctypes.c_void_p
        self.lib.sel_registerName.argtypes = [ctypes.c_char_p]
        self.lib.objc_getClass.restype = ctypes.c_void_p
        self.lib.objc_getClass.argtypes = [ctypes.c_char_p]
        # an NSRect is too big to come back in registers on Intel, where
        # it needs the _stret entry point; arm64 has no such split
        self.stret = platform.machine() == "x86_64"

    def send(self, obj, sel, restype=None, args=(), argtypes=()):
        name = ("objc_msgSend_stret" if restype is NSRect and self.stret
                else "objc_msgSend")
        fn = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p,
                              *argtypes)((name, self.lib))
        return fn(obj, self.lib.sel_registerName(sel.encode()), *args)

    def cls(self, name):
        return self.lib.objc_getClass(name.encode())

    def window_of(self, toplevel):
        """The NSWindow behind a mapped Tk toplevel, or None."""
        view = _tk_nsview(toplevel.winfo_id())
        return self.send(view, "window", ctypes.c_void_p) if view else None


class WheelMonitor:
    """An NSEvent local monitor for scroll-wheel events, made through
    ctypes alone: its handler is a global block built by hand, so the
    frozen app needs no PyObjC. The handler runs inside AppKit's event
    dispatch, under Tk's update(), so it never calls into Tcl: it only
    records each event's scroll fields and hands the event back
    unchanged."""

    SIGNATURE = b"@16@?0@8"        # id (^)(id), as clang encodes it
    FIELDS = (("deltaY", ctypes.c_double),
              ("scrollingDeltaY", ctypes.c_double),
              ("hasPreciseScrollingDeltas", ctypes.c_bool),
              ("phase", ctypes.c_ulong),
              ("momentumPhase", ctypes.c_ulong),
              ("window", ctypes.c_void_p))

    def __init__(self, objc):
        self.objc = objc
        self.got = []                  # one tuple of FIELDS per event
        self.monitor = None
        # each read made once, with its exact prototype, before any event
        self._reads = [
            (ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p)(
                ("objc_msgSend", objc.lib)),
             objc.lib.sel_registerName(sel.encode()))
            for sel, restype in self.FIELDS]
        self._invoke = EVENT_HANDLER(self._handler)
        libc = ctypes.CDLL(ctypes.util.find_library("c")
                           or "/usr/lib/libSystem.B.dylib")
        isa = ctypes.addressof(
            ctypes.c_void_p.in_dll(libc, "_NSConcreteGlobalBlock"))
        self._block = global_block(self._invoke, isa, self.SIGNATURE)

    def read(self, event):
        return tuple(fn(event, sel) for fn, sel in self._reads)

    def _handler(self, _block, event):
        try:
            if event:
                self.got.append(self.read(event))
        except Exception:
            pass                       # never let the event go
        return event

    def install(self):
        o = self.objc
        mon = o.send(o.cls("NSEvent"),
                     "addLocalMonitorForEventsMatchingMask:handler:",
                     ctypes.c_void_p,
                     (NS_EVENT_MASK_SCROLL_WHEEL,
                      ctypes.addressof(self._block)),
                     (ctypes.c_ulonglong, ctypes.c_void_p))
        if mon:
            o.send(mon, "retain", ctypes.c_void_p)
        self.monitor = mon
        return bool(mon)

    def remove(self):
        if not self.monitor:
            return
        o = self.objc
        o.send(o.cls("NSEvent"), "removeMonitor:", None, (self.monitor,),
               (ctypes.c_void_p,))
        o.send(self.monitor, "release")
        self.monitor = None


class Probe:
    out = OUT

    def __init__(self, film):
        self.film = film
        self.results = []
        self.events = Counter()
        self.keys = []
        self.choice = None
        self.bar_answer = None
        self.waiting_bar = False
        self.objc = ObjC() if IS_MAC else None

        self.root = tk.Tk()
        self.root.title("StreamSync overlay probe")
        self.root.geometry("+1080+120")
        self.root.protocol("WM_DELETE_WINDOW", lambda: self._pick("Quit"))
        self.msg = tk.Label(self.root, justify="left", wraplength=520,
                            padx=16, pady=14, font=("Helvetica", 14))
        self.msg.pack(fill="x")
        self.buttons = tk.Frame(self.root)
        self.buttons.pack(pady=(0, 14))
        self.root.bind_all("<KeyPress>", self._key)

        self.video_win = tk.Toplevel(self.root)
        self.video_win.title("StreamSync probe - film")
        self.video_win.geometry("960x540+80+120")
        self.video_win.configure(bg="black")
        self.video_frame = tk.Frame(self.video_win, bg="black")
        self.video_frame.pack(fill="both", expand=True)
        self.video_win.protocol("WM_DELETE_WINDOW", lambda: None)
        for seq, name in EVENTS:
            self.video_win.bind(seq, self._counter(name), add="+")
        self.player = None

    # ------------------------------------------------------------ plumbing

    def _counter(self, name):
        def hit(_event):
            self.events[name] += 1
        return hit

    def _key(self, event):
        self.keys.append(event.keysym)
        if self.waiting_bar and event.keysym in ("n", "N"):
            self.bar_answer = "pressed N"

    def _pick(self, choice):
        self.choice = choice

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.root.update()
            time.sleep(0.02)

    def ask(self, text, options):
        """Show text and buttons in the probe window; wait for a press."""
        self.msg.config(text=text)
        for w in self.buttons.winfo_children():
            w.destroy()
        for opt in options:
            tk.Button(self.buttons, text=opt, width=max(10, len(opt)),
                      takefocus=0, command=lambda o=opt: self._pick(o)).pack(
                side="left", padx=6)
        self.choice = None
        self.root.deiconify()
        self.root.lift()
        while self.choice is None:
            self.pump(0.05)
        if self.choice == "Quit":
            raise KeyboardInterrupt
        return self.choice

    def scrub(self, text):
        """The film is named once, on the log's film: line, and nowhere
        else - not even inside an error message."""
        if self.film:
            for name in (self.film, os.path.basename(self.film)):
                if name:
                    text = text.replace(name, "<film>")
        return text

    def note(self, line):
        if not line.startswith("film: "):
            line = self.scrub(line)
        self.results.append(line)
        print(line, flush=True)
        try:
            os.makedirs(os.path.dirname(self.out), exist_ok=True)
            with open(self.out, "w") as f:
                f.write("\n".join(self.results) + "\n")
        except OSError:
            pass

    def keys_since(self, mark):
        seen = [k for k in self.keys[mark:] if k not in ("n", "N")]
        return ", ".join(seen) if seen else "none"

    # ---------------------------------------------------------- the film

    def start_film(self, mouse_input, key_input):
        """Restart the film with libvlc's own mouse and key handling set
        each way: libvlc reads both as its video output starts."""
        mp = self.player.mp
        mp.stop()
        mp.video_set_mouse_input(bool(mouse_input))
        mp.video_set_key_input(bool(key_input))
        mp.play()
        self.pump(2.0)
        mp.audio_set_mute(True)       # a probe, not a screening

    def window_state(self):
        tk_fs = bool(int(self.video_win.attributes("-fullscreen")))
        mac_fs = None
        if self.objc:
            w = self.objc.window_of(self.video_win)
            if w:
                mask = self.objc.send(w, "styleMask", ctypes.c_ulong)
                mac_fs = bool(mask & (1 << 14))
        return f"Tk fullscreen={tk_fs}, macOS fullscreen={mac_fs}"

    def set_native_fullscreen(self, flag, timeout=4.0):
        """Tk's -fullscreen, asked again until macOS takes it: a request
        made during the fullscreen animation is dropped (see mac_app's
        _settle_fullscreen), and for an instant after a dropped request
        Tk reports the window fullscreen anyway, so it is read twice."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.video_win.attributes("-fullscreen", flag)
            self.pump(0.4)
            if bool(int(self.video_win.attributes("-fullscreen"))) == flag:
                self.pump(0.4)
                if bool(int(self.video_win.attributes("-fullscreen"))) == flag:
                    return True
        return False

    def leave_any_fullscreen(self):
        try:
            self.player.mp.set_fullscreen(False)
        except Exception:
            pass
        if bool(int(self.video_win.attributes("-fullscreen"))):
            self.set_native_fullscreen(False)
        self.pump(0.8)

    # ---------------------------------------------------------- the bar

    def make_bar(self, n):
        bar = tk.Toplevel(self.video_win)
        bar.overrideredirect(True)
        bar.attributes("-topmost", True)
        frm = tk.Frame(bar, bg=ORANGE, padx=18, pady=12)
        frm.pack()
        tk.Label(frm, text=f"Phase {n}: can you see this bar?", bg=ORANGE,
                 fg=INK, font=("Helvetica", 18, "bold")).pack(side="left")
        # a Label, not a Button: Aqua buttons ignore colours
        seen = tk.Label(frm, text="I see it", bg=INK, fg="white", padx=14,
                        pady=6, font=("Helvetica", 16, "bold"))
        seen.pack(side="left", padx=(16, 0))
        seen.bind("<Button-1>", lambda e: setattr(
            self, "bar_answer", "clicked I see it"))
        bar.update_idletasks()
        vx, vy = self.video_win.winfo_rootx(), self.video_win.winfo_rooty()
        vw, vh = self.video_win.winfo_width(), self.video_win.winfo_height()
        bw, bh = bar.winfo_reqwidth(), bar.winfo_reqheight()
        bar.geometry(f"+{vx + (vw - bw) // 2}+{vy + vh - bh - 90}")
        return bar

    def auxiliary(self, bar):
        """Mark the bar's window to join every Space, fullscreen ones
        included, above ordinary windows - how a Mac floating panel is
        normally kept over a fullscreen app."""
        o = self.objc
        w = o.window_of(bar)
        if not w:
            return "no NSWindow behind the bar"
        before = o.send(w, "collectionBehavior", ctypes.c_ulong)
        behavior = 1 | (1 << 8)        # canJoinAllSpaces | fullScreenAuxiliary
        o.send(w, "setCollectionBehavior:", None, (behavior,),
               (ctypes.c_ulong,))
        o.send(w, "setLevel:", None, (25,), (ctypes.c_long,))  # status level
        o.send(w, "orderFrontRegardless")
        return f"collectionBehavior {before:#x} -> {behavior:#x}, level 25"

    # ------------------------------------------- borderless "fullscreen"

    def enter_zoom(self):
        """Fill the screen without a fullscreen Space: the film window
        made borderless and stretched over its screen, Dock and menu bar
        hidden - the Mac cousin of what the Windows app now does."""
        o = self.objc
        w = o.window_of(self.video_win)
        if not w:
            self.note("   zoom: no NSWindow behind the film window")
            return False
        app = o.send(o.cls("NSApplication"), "sharedApplication",
                     ctypes.c_void_p)
        self._zoom = (w, app, o.send(w, "styleMask", ctypes.c_ulong),
                      o.send(w, "frame", NSRect),
                      o.send(app, "presentationOptions", ctypes.c_ulong))
        screen = o.send(w, "screen", ctypes.c_void_p)
        frame = o.send(screen, "frame", NSRect)
        # hideDock | hideMenuBar (menu bar hiding requires the Dock's)
        o.send(app, "setPresentationOptions:", None, ((1 << 1) | (1 << 3),),
               (ctypes.c_ulong,))
        o.send(w, "setStyleMask:", None, (0,), (ctypes.c_ulong,))
        o.send(w, "setFrame:display:", None, (frame, True),
               (NSRect, ctypes.c_bool))
        o.send(w, "makeKeyAndOrderFront:", None, (None,), (ctypes.c_void_p,))
        self.note(f"   zoom: screen {frame.w:.0f}x{frame.h:.0f} at "
                  f"{frame.x:.0f},{frame.y:.0f}")
        return True

    def leave_zoom(self):
        o = self.objc
        w, app, style, frame, opts = self._zoom
        o.send(w, "setStyleMask:", None, (style,), (ctypes.c_ulong,))
        o.send(w, "setFrame:display:", None, (frame, True),
               (NSRect, ctypes.c_bool))
        o.send(app, "setPresentationOptions:", None, (opts,),
               (ctypes.c_ulong,))
        return True

    # ------------------------------------------------------------ phases

    def mouse_phase(self, n, mouse_input):
        state = "ON" if mouse_input else "OFF"
        self.start_film(mouse_input, mouse_input)
        self.events.clear()
        mark = len(self.keys)
        self.ask(f"Phase {n} of {PHASES}: libvlc's own mouse handling "
                 f"{state}.\n\nOver the film window: click once, "
                 "double-click, right-click (or two-finger click), scroll "
                 "up and down, and move the mouse around. Then click the "
                 "film once more and press Space, then F.\n\n"
                 "Then press Next here.", ["Next"])
        after = self.window_state()
        reacted = self.ask("When you double-clicked, did the film react by "
                           "itself - go fullscreen, pause, anything else?",
                           ["Yes", "No"])
        seen = ", ".join(f"{name}={self.events[name]}" for _, name in EVENTS)
        self.note(f"[{n}] libvlc mouse input {state}: Tk saw {seen}")
        self.note(f"[{n}]   keys reaching Tk: {self.keys_since(mark)}")
        self.note(f"[{n}]   film reacted to the double-click by itself: "
                  f"{reacted}; afterwards {after}")
        self.leave_any_fullscreen()

    def overlay_phase(self, n, title, enter, leave, prepare=None):
        go = self.ask(f"Phase {n} of {PHASES}: {title}.\n\nThe film will "
                      "fill the screen with an orange bar near the bottom."
                      "\n\nIf you can see the bar, click 'I see it' on it. "
                      "If you can't, press N.\nIt moves on by itself after "
                      f"{BAR_WAIT:.0f} seconds.", ["Start", "Skip"])
        if go == "Skip":
            self.note(f"[{n}] {title}: skipped")
            return
        mark = len(self.keys)
        entered = enter()
        self.pump(1.2)                 # let the animation finish
        bar = self.make_bar(n)
        bar.deiconify()
        bar.lift()
        self.pump(0.2)
        how = prepare(bar) if prepare else None
        self.bar_answer = None
        self.waiting_bar = True
        end = time.monotonic() + BAR_WAIT
        while self.bar_answer is None and time.monotonic() < end:
            self.pump(0.05)
        self.waiting_bar = False
        answer = self.bar_answer or "no answer (timed out)"
        during = self.window_state()
        bar.destroy()
        leave()
        self.pump(1.0)
        saw = self.ask(f"Phase {n}: did you see the orange bar over the "
                       "full-screen film?", ["Yes", "No"])
        film = self.ask(f"Phase {n}: while it filled the screen, was the "
                        "film itself showing, picture moving?", ["Yes", "No"])
        self.note(f"[{n}] {title}: entered={entered} ({during})")
        if how:
            self.note(f"[{n}]   bar window: {how}")
        self.note(f"[{n}]   bar: {answer}; user saw it: {saw}; "
                  f"film showing: {film}")
        self.note(f"[{n}]   keys reaching Tk: {self.keys_since(mark)}")

    # ------------------------------------------------------------ the run

    def header(self, title):
        self.note(f"StreamSync {__version__} {title}, "
                  f"{time.strftime('%Y-%m-%d %H:%M')}")
        self.note(f"system: {platform.platform()} ({platform.machine()}), "
                  f"Python {platform.python_version()}, "
                  f"Tk {self.root.tk.call('info', 'patchlevel')}")
        try:
            version = vlc.libvlc_get_version().decode()
        except Exception as e:
            version = f"unknown ({e})"
        self.note(f"libvlc: {version}")

    def open_film(self):
        """Pick the film if none was given, and load it into a player
        drawing into the film window. False if there is nothing to play."""
        if not self.film:
            self.msg.config(text="Pick a film to play during the probe.")
            self.root.update()
            self.film = filedialog.askopenfilename(
                title="Pick a film for the probe")
        if not self.film:
            self.note("no film picked - nothing probed")
            return False
        self.note(f"film: {os.path.basename(self.film)}")

        self.video_win.update_idletasks()
        if IS_MAC:
            self.player = EmbeddedPlayer(None)
            if not self.player.attach_tk(self.video_frame.winfo_id()):
                self.note("attach_tk failed: no NSView - nothing to probe")
                return False
        else:
            self.player = EmbeddedPlayer(self.video_frame.winfo_id())
        self.player.load(self.film)
        return True

    def cleanup(self):
        """After a phase failed: put the film window back as it was."""
        self.leave_any_fullscreen()

    def run_phases(self, phases):
        try:
            for phase in phases:
                try:
                    phase()
                except KeyboardInterrupt:
                    raise
                except Exception:
                    self.note("   phase failed: " + traceback.format_exc()
                              .strip().splitlines()[-1])
                    diagnostics.log_block("overlay probe phase failed:",
                                          self.scrub(traceback.format_exc()))
                    self.cleanup()
            self.ask(f"Done - thank you.\n\nThe results are in\n{self.out}"
                     "\n\nand in this Terminal. Paste them back to Claude.",
                     ["Close"])
        except KeyboardInterrupt:
            self.note("stopped early by the user")
        finally:
            try:
                self.player.mp.stop()
            except Exception:
                pass
            self.root.destroy()
        return 0

    def run(self):
        self.header("overlay probe")
        if not self.open_film():
            return 1
        phases = [
            lambda: self.mouse_phase(1, True),
            lambda: self.mouse_phase(2, False),
            lambda: self.overlay_phase(
                3, "real fullscreen, plain topmost bar",
                lambda: self.set_native_fullscreen(True),
                lambda: self.set_native_fullscreen(False)),
        ]
        if self.objc:
            phases += [
                lambda: self.overlay_phase(
                    4, "real fullscreen, bar joining every Space",
                    lambda: self.set_native_fullscreen(True),
                    lambda: self.set_native_fullscreen(False),
                    prepare=self.auxiliary),
                lambda: self.overlay_phase(
                    5, "borderless screen-filling window, plain topmost bar",
                    self.enter_zoom, self.leave_zoom),
            ]
        else:
            self.note("[4] [5] skipped: macOS only")
        return self.run_phases(phases)


class SecondProbe(Probe):
    """Phases A to E (see the module docstring)."""

    out = OUT2
    # phase A's settings: (libvlc mouse input, libvlc key input)
    SPLITS = ((True, False), (False, True), (False, False))

    def __init__(self, film):
        super().__init__(film)
        self.seen = []                 # every app-wide event, in order
        self.stage = ""
        self.bar = self.bar_seen = self.track = None
        self.bar_events = []
        self.drag = self.frac = None
        self.app = None
        if self.objc:
            self.app = self.objc.send(self.objc.cls("NSApplication"),
                                      "sharedApplication", ctypes.c_void_p)
        # somewhere to scroll with no libvlc underneath (phases B and E)
        self.pad = tk.Frame(self.root, bg="#8a8d91", height=110)
        tk.Label(self.pad, text="scroll here", bg="#8a8d91", fg="white",
                 font=("Helvetica", 16, "bold")).place(
            relx=0.5, rely=0.5, anchor="center")
        self.names = {str(self.root): "probe window",
                      str(self.video_win): "film window",
                      str(self.video_frame): "film frame",
                      str(self.pad): "scroll pad"}
        for seq, kind in APP_EVENTS:
            self.root.bind_all(seq, self._seen(kind), add="+")

    # ------------------------------------------------- app-wide events

    def _seen(self, kind):
        def hit(event):
            self.record(kind, event)
        return hit

    def record(self, kind, event):
        state = event.state if isinstance(event.state, int) else 0
        delta = keysym = None
        if kind == "click" and state & CONTROL:
            kind = "ctrl_click"
        elif kind == "wheel":
            kind = "hwheel" if state & SHIFT else "wheel"
            delta = event.delta if isinstance(event.delta, int) else None
        elif kind == "key":
            keysym = event.keysym
        focus = self.who(self.root.tk.call("focus"))
        self.seen.append(Seen(time.monotonic(), kind, self.who(event.widget),
                              focus, self.key_window(), delta, keysym,
                              self.stage))
        if kind == "key" and self.bar_seen is not None:
            self.bar_seen.config(text=f"Probe saw: {keysym} (focus {focus})")

    def who(self, w):
        """A widget's name in the log: the nearest one the probe named."""
        name = "" if w is None else str(w)
        path = name
        while path:
            if path in self.names:
                return self.names[path]
            path = path.rpartition(".")[0] or ("." if path != "." else "")
        return name or "none"

    def key_window(self):
        """Which of the probe's windows AppKit has as its key window, the
        one keystrokes go to."""
        if not self.objc:
            return "n/a"
        key = self.objc.send(self.app, "keyWindow", ctypes.c_void_p)
        if not key:
            return "none"
        for name, top in (("film", self.video_win), ("bar", self.bar),
                          ("probe", self.root)):
            try:
                if (top is not None and top.winfo_ismapped()
                        and self.objc.window_of(top) == key):
                    return name
            except tk.TclError:
                pass
        return "other"

    def focus_state(self):
        active = "n/a"
        if self.objc:
            active = self.objc.send(self.app, "isActive", ctypes.c_bool)
        return (f"focus {self.who(self.root.tk.call('focus'))}, "
                f"key window {self.key_window()}, app active {active}")

    def note_mouse(self, tag, recs):
        """App-wide mouse counts, then where each kind went. The probe
        window's own buttons are left out."""
        mouse = [r for r in recs
                 if r.kind != "key" and r.target != "probe window"]
        counts = Counter(r.kind for r in mouse)
        self.note(f"{tag}   app-wide (probe window left out): "
                  + ", ".join(f"{k}={counts[k]}" for k in MOUSE_KINDS))
        for kind in MOUSE_KINDS:
            rs = [r for r in mouse if r.kind == kind]
            if rs:
                self.note(f"{tag}     {kind}: on "
                          f"{tally(r.target for r in rs)}; focus "
                          f"{tally(r.focus for r in rs)}; key window "
                          f"{tally(r.keywin for r in rs)}")
        deltas = [r.delta for r in mouse
                  if r.kind == "wheel" and r.delta is not None]
        if deltas:
            self.note(f"{tag}     wheel delta: {stats(deltas)}")

    def note_keys(self, tag, recs, label="keys"):
        keys = [r for r in recs if r.kind == "key"]
        shown = ", ".join(f"{r.keysym}->{r.target} [focus {r.focus}, "
                          f"key window {r.keywin}]" for r in keys[:12])
        more = f", and {len(keys) - 12} more" if len(keys) > 12 else ""
        self.note(f"{tag}   {label}: {shown or 'none reached Tk'}{more}")

    # ---------------------------------------------------- libvlc's view

    def libvlc_state(self):
        mp = self.player.mp
        return {"volume": mp.audio_get_volume(), "muted": mp.audio_get_mute(),
                "ms": mp.get_time(), "playing": bool(mp.is_playing()),
                "fullscreen": mp.get_fullscreen(), "wall": time.monotonic()}

    def note_libvlc(self, tag, a, b):
        moved = (b["ms"] - a["ms"]) / 1000.0
        wall = b["wall"] - a["wall"]
        self.note(f"{tag}   libvlc: volume {a['volume']} -> {b['volume']} "
                  f"(muted {a['muted']} -> {b['muted']}), playing "
                  f"{a['playing']} -> {b['playing']}, its own fullscreen "
                  f"{a['fullscreen']} -> {b['fullscreen']}")
        self.note(f"{tag}   libvlc: film time moved {moved:+.1f}s in "
                  f"{wall:.1f}s ({moved - wall:+.1f}s: a seek or a pause "
                  "shows here)")

    # --------------------------------------------------- the PR 31 bar

    def build_bar(self, steps, done):
        """A bar built like PR 31's: app-drawn label buttons and a canvas
        timeline, in an overrideredirect topmost window of its own."""
        bar = tk.Toplevel(self.video_win, bg=BAR_BG, highlightthickness=1,
                          highlightbackground="#3a3a3a")
        bar.overrideredirect(True)
        bar.attributes("-topmost", True)
        bar.attributes("-alpha", 0.92)
        bar.withdraw()
        self.names[str(bar)] = "bar"
        row = tk.Frame(bar, bg=BAR_BG)
        row.pack(fill="x", padx=14, pady=(10, 2))
        self.bar_time = tk.Label(row, width=8, anchor="e", bg=BAR_BG,
                                 fg=BAR_FG, font=("Helvetica", 12))
        self.bar_time.pack(side="left")
        self.track = tk.Canvas(row, bg=BAR_BG, height=22, cursor="hand2",
                               highlightthickness=0)
        self.track.pack(side="left", fill="x", expand=True, padx=10)
        self.names[str(self.track)] = "bar timeline"
        self.bar_len = tk.Label(row, width=8, anchor="w", bg=BAR_BG,
                                fg=BAR_FG, font=("Helvetica", 12))
        self.bar_len.pack(side="left")
        row = tk.Frame(bar, bg=BAR_BG)
        row.pack(fill="x", padx=14, pady=(2, 4))
        self.bar_button(row, "⏯ Play/Pause", "Play/Pause",
                        self.player.mp.pause)
        self.bar_button(row, "⟲ Resync here", "Resync here", None)
        self.bar_button(row, done, done, lambda: setattr(
            self, "bar_answer", f"clicked {done}"))
        self.bar_seen = tk.Label(row, anchor="w", bg=BAR_BG, fg="#9a9a9a",
                                 font=("Helvetica", 12),
                                 text="Probe saw: nothing yet")
        self.bar_seen.pack(side="left", fill="x", expand=True, padx=(8, 0))
        tk.Label(bar, text=steps, anchor="w", bg=BAR_BG, fg=ORANGE,
                 font=("Helvetica", 13, "bold")).pack(
            fill="x", padx=14, pady=(0, 10))
        self.track.bind("<ButtonPress-1>", self.track_press)
        self.track.bind("<B1-Motion>", self.track_drag)
        self.track.bind("<ButtonRelease-1>", self.track_release)
        self.track.bind("<Configure>", lambda e: self.draw_track())
        self.bar = bar
        self.bar_events = []
        self.drag = self.frac = None
        return bar

    def bar_button(self, parent, text, name, action):
        # a Label, not a Button: Aqua buttons ignore colours
        b = tk.Label(parent, text=text, bg=BAR_KEY, fg=BAR_FG, padx=12,
                     pady=3, font=("Helvetica", 13), cursor="hand2")
        b.pack(side="left", padx=(0, 6))
        b.bind("<Enter>", lambda e: b.config(bg="#444444"))
        b.bind("<Leave>", lambda e: b.config(bg=BAR_KEY))
        # deferred, as PR 31 defers every mouse handler over the film
        b.bind("<Button-1>", lambda e: self.video_win.after(
            1, lambda: self.bar_clicked(name, action)))
        self.names[str(b)] = f"bar button {name}"

    def bar_clicked(self, name, action):
        if action:
            action()
        self.stage = f"after clicking {name}"
        self.bar_events.append(f"clicked {name}: {self.focus_state()}")
        if self.bar_answer is None:    # not the click that ends the phase
            self.video_win.after(400, lambda: self.bar_events.append(
                f"  0.4 s after clicking {name}: {self.focus_state()}"))
        if self.bar_seen is not None:
            self.bar_seen.config(text=f"Probe saw: click on {name}")

    def track_press(self, event):
        self.drag = 0
        self.frac = self.frac_at(event.x)
        self.draw_track()
        self.bar_events.append(f"timeline pressed: {self.focus_state()}")

    def track_drag(self, event):
        if self.drag is not None:
            self.drag += 1
            self.frac = self.frac_at(event.x)
            self.draw_track()

    def track_release(self, event):
        if self.drag is None:
            return
        moves, self.drag = self.drag, None
        frac = self.frac = self.frac_at(event.x)
        self.draw_track()
        self.video_win.after(1, lambda: self.player.mp.set_position(frac))
        self.stage = "after the timeline drag"
        self.bar_events.append(f"timeline released at {frac:.0%} after "
                               f"{moves} moves: {self.focus_state()}")
        self.video_win.after(400, lambda: self.bar_events.append(
            f"  0.4 s after the drag: {self.focus_state()}"))
        if self.bar_seen is not None:
            self.bar_seen.config(text="Probe saw: timeline drag")

    def frac_at(self, x):
        w = self.track.winfo_width()
        return min(1.0, max(0.0, (x - 8) / max(1, w - 16)))

    def draw_track(self):
        c = self.track
        if c is None:
            return
        c.delete("all")
        y = c.winfo_height() // 2
        x0, x1 = 8, max(8, c.winfo_width() - 8)
        c.create_line(x0, y, x1, y, fill="#4a4a4a", width=4, capstyle="round")
        if self.frac is not None:
            x = x0 + (x1 - x0) * self.frac
            c.create_line(x0, y, x, y, fill="#ff8800", width=4,
                          capstyle="round")
            c.create_oval(x - 7, y - 7, x + 7, y + 7, fill="#f2f2f2",
                          outline="")

    def refresh_bar(self):
        mp = self.player.mp
        t, n = mp.get_time(), mp.get_length()
        self.bar_time.config(text=clock(t))
        self.bar_len.config(text=clock(n))
        if self.drag is None:
            self.frac = (min(1.0, max(0.0, t / n)) if t >= 0 and n > 0
                         else None)
            self.draw_track()

    def show_bar(self, bar):
        """PR 31's placement: centred near the bottom of the film window."""
        win = self.video_win
        bar.update_idletasks()
        w = max(480, min(1100, win.winfo_width() - 80))
        h = bar.winfo_reqheight()
        x = win.winfo_rootx() + (win.winfo_width() - w) // 2
        y = win.winfo_rooty() + win.winfo_height() - h - 40
        bar.geometry(f"{w}x{h}+{x}+{y}")
        bar.deiconify()
        bar.lift()

    def wait_bar(self, seconds):
        """Until the bar's last button, N, or the time runs out."""
        self.bar_answer = None
        self.waiting_bar = True
        end = time.monotonic() + seconds
        tick = 0.0
        while self.bar_answer is None and time.monotonic() < end:
            if time.monotonic() >= tick:
                self.refresh_bar()
                tick = time.monotonic() + 0.25
            self.pump(0.05)
        self.waiting_bar = False

    def close_bar(self):
        if self.bar is not None:
            try:
                self.bar.destroy()
            except tk.TclError:
                pass
        self.bar = self.bar_seen = self.track = None
        self.drag = None

    def cleanup(self):
        # the bar joins every Space: it goes before the fullscreen does
        self.close_bar()
        self.pad.pack_forget()
        self.stage = ""
        super().cleanup()

    # ---------------------------------------------------------- screens

    def screens(self):
        """Every screen's frame, the menu bar's first (Cocoa coordinates)."""
        o = self.objc
        arr = o.send(o.cls("NSScreen"), "screens", ctypes.c_void_p)
        n = o.send(arr, "count", ctypes.c_ulong) if arr else 0
        return [o.send(o.send(arr, "objectAtIndex:", ctypes.c_void_p, (i,),
                              (ctypes.c_ulong,)), "frame", NSRect)
                for i in range(n)]

    def screen_of(self, top, screens):
        """Which screen AppKit has a window on (the one holding most of
        it), and the window's frame."""
        o = self.objc
        w = o.window_of(top)
        if not w:
            return "no NSWindow"
        f = o.send(w, "frame", NSRect)
        where = f"frame {f.w:.0f}x{f.h:.0f} at {f.x:.0f},{f.y:.0f}"
        s = o.send(w, "screen", ctypes.c_void_p)
        if not s:
            return f"no screen ({where})"
        sf = o.send(s, "frame", NSRect)
        for i, r in enumerate(screens, 1):
            if (abs(r.x - sf.x) < 1 and abs(r.y - sf.y) < 1
                    and abs(r.w - sf.w) < 1 and abs(r.h - sf.h) < 1):
                return f"screen {i} ({where})"
        return f"a screen not in the list ({where})"

    @staticmethod
    def spaces_setting():
        """System Settings' "Displays have separate Spaces", which
        changes how fullscreen Spaces behave with two screens."""
        try:
            r = subprocess.run(["/usr/bin/defaults", "read",
                                "com.apple.spaces", "spans-displays"],
                               capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as e:
            return f"could not read ({e})"
        if r.returncode != 0:
            return "on (spans-displays not set, the default)"
        v = r.stdout.strip()
        meaning = {"0": "on", "1": "off"}.get(v, "?")
        return f"{meaning} (spans-displays={v})"

    # ------------------------------------------------------------ phases

    def input_split(self, i, mouse, key):
        tag = f"[A{i}]"
        label = (f"libvlc mouse input {'ON' if mouse else 'OFF'}, "
                 f"key input {'ON' if key else 'OFF'}")
        self.start_film(mouse, key)
        self.player.mp.audio_set_volume(80)   # room to move either way
        self.events.clear()
        mark = len(self.seen)
        before = self.libvlc_state()
        self.ask(f"Phase A, part {i} of 3: {label}.\n\n"
                 "Over the film window, in this order:\n"
                 "1. Click the film once.\n"
                 "2. Press Space, then F.\n"
                 "3. Double-click the film.\n"
                 "4. Scroll up and down with the trackpad, and with a "
                 "mouse wheel too if you have one.\n\n"
                 "Then press Next here.", ["Next"])
        after = self.libvlc_state()
        recs = self.seen[mark:]
        film = ", ".join(f"{name}={self.events[name]}" for _, name in EVENTS)
        state = self.window_state()
        reacted = self.ask(f"Phase A, part {i}: did the film react by "
                           "itself to any of that - pause, go full screen, "
                           "jump?", ["Yes", "No"])
        self.note(f"{tag} {label}")
        self.note(f"{tag}   film window bindings: {film}")
        self.note_mouse(tag, recs)
        self.note_keys(tag, recs)
        self.note_libvlc(tag, before, after)
        self.note(f"{tag}   film reacted by itself: {reacted}; "
                  f"afterwards {state}")
        self.leave_any_fullscreen()

    def scroll_sizes(self):
        steps = (
            ("B1", "trackpad, slow scroll up",
             "With two fingers on the trackpad, scroll up slowly over the "
             "grey pad below, about one finger-length.", ["Next"]),
            ("B2", "trackpad, quick flick down",
             "Now a quick two-finger flick down over the grey pad, and let "
             "it coast to a stop.", ["Next"]),
            ("B3", "mouse wheel, 5 notches up",
             "If you have a mouse with a wheel: over the grey pad, roll it "
             "up 5 notches, one at a time. No mouse: press No mouse.",
             ["Next", "No mouse"]),
        )
        self.pad.pack(fill="x", padx=16, pady=(0, 14), before=self.buttons)
        try:
            for tag, what, text, options in steps:
                mark = len(self.seen)
                got = self.ask(f"Phase B of {PHASES}: scroll sizes.\n\n"
                               f"{text}\n\nThen press Next.", options)
                if got == "No mouse":
                    self.note(f"[{tag}] {what}: no mouse at hand")
                    continue
                recs = [r for r in self.seen[mark:]
                        if r.kind in ("wheel", "hwheel")]
                v = [r.delta for r in recs
                     if r.kind == "wheel" and r.delta is not None]
                span = recs[-1].t - recs[0].t if len(recs) > 1 else 0.0
                first = " ".join(str(d) for d in v[:12]) or "-"
                self.note(f"[{tag}] {what}: delta {stats(v)}; over "
                          f"{span:.1f}s; sideways "
                          f"{sum(r.kind == 'hwheel' for r in recs)}; on "
                          f"{tally(r.target for r in recs)}; first: {first}")
        finally:
            self.pad.pack_forget()

    def bar_keys(self):
        go = self.ask(
            f"Phase C of {PHASES}: keys after using the bar.\n\n"
            "The film will fill the screen with a dark control bar near "
            "the bottom. On it, in this order:\n"
            "1. Click ⏯ Play/Pause.\n"
            "2. Press Space, then F.\n"
            "3. Drag the timeline's dot a little way.\n"
            "4. Press Space, then F.\n"
            "5. Click Done.\n\n"
            "The bar lists these steps and shows what the probe saw. Watch "
            "whether the menu bar or the Dock appears when you click the "
            f"bar. It moves on by itself after {BAR_KEYS_WAIT:.0f} seconds.",
            ["Start", "Skip"])
        if go == "Skip":
            self.note("[C] keys after using the bar: skipped")
            return
        self.start_film(False, False)
        entered = self.set_native_fullscreen(True)
        self.pump(1.2)                 # let the animation finish
        bar = self.build_bar("1 Click ⏯ Play/Pause    2 Space, F    "
                             "3 Drag the timeline    4 Space, F    "
                             "5 Click Done", "Done")
        self.show_bar(bar)
        self.pump(0.3)
        self.stage = "before touching the bar"
        start = self.focus_state()
        mark = len(self.seen)
        self.wait_bar(BAR_KEYS_WAIT)
        recs = self.seen[mark:]
        during = self.window_state()
        answer = self.bar_answer or "no answer (timed out)"
        self.close_bar()               # before the fullscreen goes
        self.stage = ""
        self.set_native_fullscreen(False)
        self.pump(1.0)
        menu = self.ask("Phase C: when you clicked the bar, did the menu "
                        "bar or the Dock appear?",
                        ["Neither", "Menu bar", "Dock", "Both"])
        self.note("[C] real fullscreen, PR 31-style bar, libvlc input OFF: "
                  f"entered={entered} ({during})")
        self.note(f"[C]   before touching the bar: {start}")
        for line in self.bar_events:
            self.note(f"[C]   {line}")
        self.note_mouse("[C]", recs)
        stages = list(dict.fromkeys(r.stage for r in recs if r.kind == "key"))
        if not stages:
            self.note("[C]   keys: none reached Tk")
        for stage in stages:
            self.note_keys("[C]", [r for r in recs if r.stage == stage],
                           f"keys {stage}")
        self.note(f"[C]   ended: {answer}; menu bar or Dock appeared after "
                  f"the click: {menu}")

    def second_screen(self):
        self.note(f"[D] Displays have separate Spaces: "
                  f"{self.spaces_setting()}")
        screens = self.screens()
        listed = "; ".join(f"{i}: {r.w:.0f}x{r.h:.0f} at {r.x:.0f},{r.y:.0f}"
                           for i, r in enumerate(screens, 1))
        self.note(f"[D] screens: {len(screens)} ({listed}; 1 has the menu "
                  "bar; Cocoa coordinates)")
        if len(screens) < 2:
            self.note("[D] second screen: skipped, only one screen attached")
            return
        go = self.ask(
            f"Phase D of {PHASES}: second screen.\n\n"
            "Drag the film window by its title bar onto your second "
            "screen, so that it sits wholly on it. Then press Next.\n\n"
            "The film will then fill a screen, with the control bar near "
            "the bottom. Watch which screen. If you see the bar, click "
            "'I see it' on it; if you don't, press N. It moves on by "
            f"itself after {BAR_WAIT:.0f} seconds.", ["Next", "Skip"])
        if go == "Skip":
            self.note("[D] second screen: skipped")
            return
        before = self.screen_of(self.video_win, screens)
        entered = self.set_native_fullscreen(True)
        self.pump(1.2)                 # let the animation finish
        filled = self.screen_of(self.video_win, screens)
        bar = self.build_bar("Can you see this bar? Click I see it, or "
                             "press N if you can't.", "I see it")
        self.show_bar(bar)
        self.pump(0.3)
        bar_on = self.screen_of(bar, screens)
        self.wait_bar(BAR_WAIT)
        during = self.window_state()
        answer = self.bar_answer or "no answer (timed out)"
        self.close_bar()               # before the fullscreen goes
        self.stage = ""
        self.set_native_fullscreen(False)
        self.pump(1.5)
        after = self.screen_of(self.video_win, screens)
        which = self.ask("Phase D: did the film fill the screen you dragged "
                         "it to?", ["Yes", "No, the other", "Neither"])
        saw = self.ask("Phase D: where did you see the bar?",
                       ["Over the film", "Other screen", "Nowhere"])
        back = self.ask("Phase D: now that it has left full screen, is the "
                        "film window back on the screen you dragged it to?",
                        ["Yes", "No"])
        self.note(f"[D] film window before: {before}")
        self.note(f"[D]   fullscreen entered={entered} ({during}): film "
                  f"window on {filled}")
        self.note(f"[D]   bar window on {bar_on}; bar: {answer}")
        for line in self.bar_events:
            self.note(f"[D]   {line}")
        self.note(f"[D]   after leaving fullscreen: film window on {after}")
        self.note(f"[D]   user: filled the chosen screen: {which}; bar seen: "
                  f"{saw}; back on the chosen screen: {back}")

    def native_monitor(self):
        go = self.ask(
            f"Phase E of {PHASES}, the last: a native scroll-wheel "
            "listener.\n\nThe probe now listens to the scroll wheel the way "
            "a native Mac app can. If StreamSync quits suddenly here, that "
            "is an answer too: the log keeps everything so far.\n\n"
            "Press Start, and the next step says what to do.",
            ["Start", "Skip"])
        if go == "Skip":
            self.note("[E] NSEvent scroll-wheel monitor: skipped")
            return
        self.start_film(False, False)
        self.events.clear()
        self.note("[E] NSEvent scroll-wheel monitor, libvlc input OFF: "
                  "installing (a log that ends here means that crashed)")
        monitor = WheelMonitor(self.objc)
        _KEEP.append(monitor)
        installed = monitor.install()
        self.note(f"[E]   installed: {installed}")
        if not installed:
            return
        self.pad.pack(fill="x", padx=16, pady=(0, 14), before=self.buttons)
        mark = len(self.seen)
        try:
            self.ask(f"Phase E of {PHASES}: native scroll-wheel listener."
                     "\n\n1. Over the film window, scroll up and down with "
                     "the trackpad, and with a mouse wheel too if you have "
                     "one.\n2. Then scroll over the grey pad below.\n\n"
                     "Then press Next.", ["Next"])
        finally:
            monitor.remove()
            self.pad.pack_forget()
        got = list(monitor.got)
        recs = self.seen[mark:]
        film_w = self.objc.window_of(self.video_win)
        probe_w = self.objc.window_of(self.root)

        def where(w):
            if not w:
                return "no window"
            return ("film window" if w == film_w else
                    "probe window" if w == probe_w else "other window")

        self.note(f"[E]   monitor saw {len(got)} scroll events: on "
                  f"{tally(where(g[5]) for g in got)}")
        for name in ("film window", "probe window"):
            part = [g for g in got if where(g[5]) == name]
            if not part:
                continue
            self.note(f"[E]     {name}: deltaY "
                      f"{stats([g[0] for g in part])}; scrollingDeltaY "
                      f"{stats([g[1] for g in part])}")
            self.note(f"[E]     {name}: precise "
                      f"{sum(1 for g in part if g[2])}, gesture phase "
                      f"{sum(1 for g in part if g[3])}, momentum "
                      f"{sum(1 for g in part if g[4])}, neither (a plain "
                      f"wheel) {sum(1 for g in part if not (g[3] or g[4]))}")
        wheel = [r for r in recs if r.kind in ("wheel", "hwheel")]
        self.note(f"[E]   Tk meanwhile: wheel {len(wheel)} app-wide (on "
                  f"{tally(r.target for r in wheel)}); film window "
                  f"bindings wheel={self.events['wheel']}")

    def run(self):
        self.header("overlay probe 2")
        if not self.open_film():
            return 1
        phases = [lambda i=i, m=m, k=k: self.input_split(i, m, k)
                  for i, (m, k) in enumerate(self.SPLITS, 1)]
        phases += [self.scroll_sizes, self.bar_keys]
        if self.objc:
            phases += [self.second_screen, self.native_monitor]
        else:
            self.note("[D] [E] skipped: macOS only")
        return self.run_phases(phases)


def main(argv):
    films = [a for a in argv[1:] if not a.startswith("--")]
    probe = Probe if "--first" in argv else SecondProbe
    return probe(films[0] if films else None).run()
