"""Field probe for the Mac film window - a one-off, not part of the app.

Two questions decide how the next StreamSync gives the Mac the film
window's mouse controls and its fullscreen control bar, and neither can
be answered from a PC:

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

Run it from Terminal against a built app:

    .../StreamSync.app/Contents/MacOS/StreamSync --probe-overlay [film]

It walks through five short phases in a window of its own and rewrites
~/Library/Logs/StreamSync/overlay-probe.txt after every phase, so even a
crash leaves the answers so far. It never reads or writes the app's
settings.
"""

import ctypes
import ctypes.util
import os
import platform
import sys
import time
import tkinter as tk
import traceback
from collections import Counter
from tkinter import filedialog

import diagnostics
from players import EmbeddedPlayer, _tk_nsview, vlc
from version import __version__

IS_MAC = sys.platform == "darwin"
OUT = os.path.join(diagnostics.LOG_DIR, "overlay-probe.txt")
ORANGE, INK = "#ff8a1f", "#17181a"
BAR_WAIT = 20.0            # seconds a fullscreen phase waits for an answer
PHASES = 5

# What the film window listens for. On Aqua Tk 8.6 a right-click arrives
# as Button-2 (and as Control-Button-1 from a one-button mouse); Button-3
# is listed in case this Tk numbers buttons the other way.
EVENTS = (("<Button-1>", "click"), ("<Double-Button-1>", "double"),
          ("<Button-2>", "button2"), ("<Button-3>", "button3"),
          ("<Control-Button-1>", "ctrl_click"), ("<MouseWheel>", "wheel"),
          ("<Motion>", "motion"))


class NSRect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double),
                ("w", ctypes.c_double), ("h", ctypes.c_double)]


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


class Probe:
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
            tk.Button(self.buttons, text=opt, width=10, takefocus=0,
                      command=lambda o=opt: self._pick(o)).pack(
                side="left", padx=6)
        self.choice = None
        self.root.deiconify()
        self.root.lift()
        while self.choice is None:
            self.pump(0.05)
        if self.choice == "Quit":
            raise KeyboardInterrupt
        return self.choice

    def note(self, line):
        self.results.append(line)
        print(line, flush=True)
        try:
            os.makedirs(os.path.dirname(OUT), exist_ok=True)
            with open(OUT, "w") as f:
                f.write("\n".join(self.results) + "\n")
        except OSError:
            pass

    def keys_since(self, mark):
        seen = [k for k in self.keys[mark:] if k not in ("n", "N")]
        return ", ".join(seen) if seen else "none"

    # ---------------------------------------------------------- the film

    def start_film(self, mouse_input):
        mp = self.player.mp
        mp.stop()
        mp.video_set_mouse_input(bool(mouse_input))
        mp.video_set_key_input(bool(mouse_input))
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
        self.start_film(mouse_input)
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

    def run(self):
        self.note(f"StreamSync {__version__} overlay probe, "
                  f"{time.strftime('%Y-%m-%d %H:%M')}")
        self.note(f"system: {platform.platform()} ({platform.machine()}), "
                  f"Python {platform.python_version()}, "
                  f"Tk {self.root.tk.call('info', 'patchlevel')}")
        try:
            version = vlc.libvlc_get_version().decode()
        except Exception as e:
            version = f"unknown ({e})"
        self.note(f"libvlc: {version}")

        if not self.film:
            self.msg.config(text="Pick a film to play during the probe.")
            self.root.update()
            self.film = filedialog.askopenfilename(
                title="Pick a film for the probe")
        if not self.film:
            self.note("no film picked - nothing probed")
            return 1
        self.note(f"film: {os.path.basename(self.film)}")

        self.video_win.update_idletasks()
        if IS_MAC:
            self.player = EmbeddedPlayer(None)
            if not self.player.attach_tk(self.video_frame.winfo_id()):
                self.note("attach_tk failed: no NSView - nothing to probe")
                return 1
        else:
            self.player = EmbeddedPlayer(self.video_frame.winfo_id())
        self.player.load(self.film)

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
                                          traceback.format_exc())
                    self.leave_any_fullscreen()
            self.ask(f"Done - thank you.\n\nThe results are in\n{OUT}\n\n"
                     "and in this Terminal. Paste them back to Claude.",
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


def main(argv):
    films = [a for a in argv[1:] if not a.startswith("--")]
    return Probe(films[0] if films else None).run()
