"""Live check of the Mac shell: the real MacApp over real Tk, real libvlc
and real System Events, driven through the methods its menus and keys call.

test_macswap.py covers the same ground over a fake Tk, and it passed on
v1.0.8 - whose first run on a Mac, with Automation refused, lost
fullscreen on every other pause. macOS drops a fullscreen request made
while the window is still animating, and no fake knew that until this
found it. It is kept so the next change to the swap can be run against a
real screen:

    python3 test_macapp_live.py [/path/to/film]

It takes over the screen for about two minutes. The film goes in and out
of fullscreen, a small helper window stands in for the stream's browser
(no real browser is touched), and VLC opens for the External VLC steps
and is closed again. It needs VLC, Automation permission for System
Events, and a film: the one StreamSync last had open, or the path given.
Settings and the log go to a temporary folder; ~/.streamsync.json is read
for the film's path and never written.

  A  pause and resume with the film fullscreen - by key, by the stream's
     own pause events, and with one landing inside the other's animation
  B  the same with Automation refused. The refusal is injected where the
     shell calls System Events, with the error macOS gives for one
     (-1743): revoking the real permission is not a test's to do
  C  External VLC and back, with a film opened and the film window closed
     meanwhile
  D  fullscreen asked for twice inside one animation

The picture itself is not looked at (that would need Screen Recording
permission): "plays" means libvlc reports a video output in our window.
Not in CI - it needs a display, VLC and a film.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

if sys.platform != "darwin":
    print(f"SKIPPED: the Mac shell is macOS-only, this is {sys.platform}")
    raise SystemExit(0)

import tkinter as tk                      # noqa: E402

import controller                         # noqa: E402
import diagnostics                        # noqa: E402
import macwindowctl                       # noqa: E402
import mac_app                            # noqa: E402
import test_macwindowctl as helper_mod    # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
APP = helper_mod.APP                      # the stand-in stream app's name
REFUSAL = ("36:92: execution error: Not authorized to send Apple events to "
           "System Events. (-1743)")


def find_film():
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        try:
            path = json.loads((Path.home() / ".streamsync.json")
                              .read_text()).get("video_path")
        except (OSError, ValueError, AttributeError):
            path = None
    if not path or not os.path.isfile(path):
        sys.exit("FAILED: no film to test with - open one in StreamSync "
                 "once, or pass its path: python3 test_macapp_live.py "
                 "/path/to/film.mkv")
    return path


FILM = find_film()
helper_mod.check_permission(macwindowctl)

# The app's settings and log, kept away from the real ones.
WORK = tempfile.mkdtemp(prefix="streamsync-live-")
controller.CONFIG_PATH = Path(WORK) / "streamsync.json"
controller.CONFIG_PATH.write_text(json.dumps({"video_path": FILM,
                                              "swap": True}))
diagnostics.LOG_DIR = WORK
diagnostics.LOG_FILE = os.path.join(WORK, "streamsync.log")

T0 = time.time()
results = []          # one dict per step
errors = []           # exceptions from Tk callbacks and threads
dialogs = []          # message boxes the app tried to show
timeline = []         # the state four times a second, for a failure
calls = []            # the app's own System Events calls: (t, kind, refused)
mode = {"refuse": False}


def now():
    return round(time.time() - T0, 2)


# ---------------------------------------------------------------- recording

_osascript = macwindowctl._osascript


def _recording_osascript(script, timeout=6.0):
    if "unix id" in script:
        kind = "activate_self"
    elif "set frontmost to true" in script:
        kind = "activate_app"
    elif "to false" in script:
        kind = "hide_app"
    elif "every application process" in script:
        kind = "list_gui_apps"
    else:
        kind = "other"
    calls.append((now(), kind, mode["refuse"]))
    if mode["refuse"]:
        time.sleep(0.25)          # about what a refused round trip costs
        raise RuntimeError(REFUSAL)
    return _osascript(script, timeout)


macwindowctl._osascript = _recording_osascript


def _box(kind):
    # A real dialog would block the run on a click nobody is there to make.
    def show(title, message=None, **kw):
        dialogs.append((now(), kind, str(title), str(message)[:300]))
        return "ok"
    return show


for _kind in ("showerror", "showinfo", "showwarning"):
    setattr(mac_app.messagebox, _kind, _box(_kind))


def _thread_hook(args):
    errors.append((now(), "thread", "".join(traceback.format_exception(
        args.exc_type, args.exc_value, args.exc_traceback))[-1500:]))


threading.excepthook = _thread_hook

# What System Events sees, sampled off the Tk thread: asking takes a
# moment, and asked from the Tk thread it would hold up the very event
# loop being measured.
seen = {"t": None, "front": None, "helper_visible": None,
        "helper_front": None, "mine_front": None}
_stop_sampler = threading.Event()
_SAMPLE = f'''tell application "System Events"
set f to name of first application process whose frontmost is true
try
set hv to visible of application process "{APP}"
set hf to frontmost of application process "{APP}"
on error
set hv to "gone"
set hf to "gone"
end try
set mf to frontmost of (first application process whose unix id is {os.getpid()})
return f & "|" & hv & "|" & hf & "|" & mf
end tell'''


def _sampler():
    while not _stop_sampler.is_set():
        try:
            r = subprocess.run(["osascript", "-e", _SAMPLE],
                               capture_output=True, text=True, timeout=5)
            f, hv, hf, mf = r.stdout.strip().split("|")
            seen.update(t=now(), front=f, helper_visible=hv == "true",
                        helper_front=hf == "true", mine_front=mf == "true")
        except Exception as e:
            seen.update(t=now(), front=f"ERR {e}"[:60])
        _stop_sampler.wait(0.15)


# ------------------------------------------------------------------ the app

root = tk.Tk()


def _tk_error(exc, val, tb):
    errors.append((now(), "tk", "".join(
        traceback.format_exception(exc, val, tb))[-1500:]))


root.report_callback_exception = _tk_error
app = mac_app.MacApp(root)
app._check_updates = lambda quiet=True: None     # no network, no dialogs
SW, SH = root.winfo_screenwidth(), root.winfo_screenheight()


def state():
    vw = app.video_win
    vw.update_idletasks()
    mp = app.player_backend.mp
    w, h = vw.winfo_width(), vw.winfo_height()
    return {
        "t": now(),
        "playing": bool(app.player.is_playing()),
        "embedded_playing": bool(mp.is_playing()),
        "fs_flag": app.fullscreen,                  # what the app believes
        "fs_attr": bool(int(vw.attributes("-fullscreen"))),   # ...Tk says
        "fills_screen": w >= SW and h >= 0.93 * SH,           # ...and is
        "win": [w, h],
        "win_state": vw.state(),
        "has_vout": mp.has_vout(),
        "swapped": app._swapped,
        "swap_target": app._swap_target,
        "owes_fullscreen": app._was_fullscreen,
        "status": app.status_lbl.cget("text"),
        "front": seen["front"],
        "helper_visible": seen["helper_visible"],
        "helper_front": seen["helper_front"],
        "mine_front": seen["mine_front"],
    }


def _tick():
    try:
        s = state()
        timeline.append({k: s[k] for k in (
            "t", "playing", "fs_flag", "fs_attr", "fills_screen", "win_state",
            "front", "helper_visible", "mine_front", "owes_fullscreen")})
    except Exception:
        pass
    root.after(250, _tick)


def check(step, want, note=""):
    """Record a step: the state now, against what it should be."""
    got = state()
    wrong = {}
    for key, expected in want.items():
        if callable(expected):
            if not expected(got.get(key)):
                wrong[key] = f"{got.get(key)!r}, wanted {expected.__doc__}"
        elif got.get(key) != expected:
            wrong[key] = f"{got.get(key)!r}, wanted {expected!r}"
    since = results[-1]["t"] if results else 0
    results.append({"step": step, "t": got["t"], "ok": not wrong,
                    "wrong": wrong, "note": note, "state": got,
                    "app_osascript": [c for c in calls if c[0] > since]})
    print(f"[{got['t']:6.1f}s] {'ok  ' if not wrong else 'FAIL'} {step}"
          + (f"  <- {wrong}" if wrong else ""), flush=True)


def until(cond, timeout, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return
        except Exception:
            pass
        yield step


def contains(text):
    def f(v):
        return text in (v or "")
    f.__doc__ = f"text containing {text!r}"
    return f


def at_least_one(v):
    """at least 1"""
    return isinstance(v, int) and v >= 1


def status():
    return app.status_lbl.cget("text")


FULL = {"fs_flag": True, "fs_attr": True, "fills_screen": True}
WINDOWED = {"fs_flag": False, "fs_attr": False, "fills_screen": False}
FILM_FRONT = {"helper_visible": False, "mine_front": True}
STREAM_FRONT = {"helper_visible": True, "helper_front": True}

helper_proc = None


def script():
    global helper_proc
    yield 1.0

    # the stand-in for the stream's browser
    helper = helper_mod.make_helper_app(WORK)
    env = dict(os.environ, __PYVENV_LAUNCHER__=sys.executable)
    helper_proc = subprocess.Popen(
        [helper, "-c", helper_mod.HELPER], env=env, cwd=HERE,
        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    threading.Thread(target=_sampler, daemon=True).start()
    yield from until(lambda: seen["helper_visible"] is not None, 15)
    app.stream_app = APP
    app.streamapp_var.set(APP)
    app._swap_app = ""

    # --- playback, the way a first sync starts it ---------------------
    app.ctl.set_mute(True)
    started = {}

    def start():
        try:
            app.player.ensure_playing()
            n = app.player.length()
            app.player.seek(min(600.0, n / 3) if n else 60.0)
            started["ok"] = True
        except Exception as e:
            started["err"] = repr(e)
    threading.Thread(target=start, daemon=True).start()
    yield from until(lambda: started, 10)
    yield 2.5
    check("0  film plays in its window", dict(
        playing=True, has_vout=at_least_one, win_state="normal", **WINDOWED),
        note=str(started))

    # --- A: pause / resume with the film fullscreen --------------------
    app._toggle_fullscreen()
    yield 3.0
    check("A1 fullscreen on", dict(
        playing=True, has_vout=at_least_one, **FULL))

    app._toggle_pause()
    yield 4.0
    check("A2 pause: film leaves fullscreen, stream app comes forward", dict(
        playing=False, owes_fullscreen=True, swapped=True,
        **WINDOWED, **STREAM_FRONT))

    app._toggle_pause()
    yield 5.0
    check("A3 resume: stream app hidden, film in front, fullscreen back",
          dict(playing=True, owes_fullscreen=False, swapped=False,
               has_vout=at_least_one, **FULL, **FILM_FRONT))

    app._toggle_pause()
    yield 0.5
    app._toggle_pause()
    yield 6.0
    check("A4 pause then resume 0.5 s apart: ends playing, fullscreen", dict(
        playing=True, owes_fullscreen=False, **FULL, **FILM_FRONT))

    app._toggle_pause()
    yield 4.0
    app._toggle_pause()
    yield 0.5
    app._toggle_pause()
    yield 6.0
    check("A5 resume then pause 0.5 s apart: ends paused, stream app up",
          dict(playing=False, owes_fullscreen=True,
               **WINDOWED, **STREAM_FRONT))

    app._toggle_pause()
    yield 5.0
    check("A6 resume after that: fullscreen back", dict(
        playing=True, owes_fullscreen=False, **FULL, **FILM_FRONT))

    # the stream's own pauses arrive as queue events, not key presses
    app.q.put(("swap", True))
    yield 4.0
    check("A7 stream pause event: stream app forward, film windowed", dict(
        owes_fullscreen=True, **WINDOWED, **STREAM_FRONT))
    app.q.put(("swap", False))
    yield 5.0
    check("A8 stream resume event: film fullscreen and in front", dict(
        owes_fullscreen=False, **FULL, **FILM_FRONT))

    # a pause that lands while the film is still entering fullscreen:
    # macOS drops the request to leave, and a browser raised before the
    # film does leave ends up behind it
    app.q.put(("swap", True))
    yield 4.0
    app.q.put(("swap", False))
    yield from until(lambda: app.fullscreen, 4, step=0.02)
    app.q.put(("swap", True))
    yield 6.0
    check("A9 stream pauses the instant fullscreen is handed back: "
          "stream app ends in front, film windowed", dict(
              owes_fullscreen=True, **WINDOWED, **STREAM_FRONT))
    app.q.put(("swap", False))
    yield 5.0
    check("A10 and resumes: film fullscreen and in front", dict(
        owes_fullscreen=False, **FULL, **FILM_FRONT))

    # ...and a pause like that one, overtaken by its resume while it is
    # still waiting for the film to leave: it is called off
    app.q.put(("swap", True))
    yield 4.0
    app.q.put(("swap", False))
    yield from until(lambda: app.fullscreen, 4, step=0.02)
    app.q.put(("swap", True))
    yield 0.15
    app.q.put(("swap", False))
    yield 6.0
    check("A11 pause and resume straight after fullscreen is handed back: "
          "film stays fullscreen and in front", dict(
              owes_fullscreen=False, swapped=False, **FULL, **FILM_FRONT))

    # --- B: Automation refused ----------------------------------------
    mode["refuse"] = True
    app._toggle_pause()
    yield 4.0
    check("B1 pause, refused: says so, gives fullscreen back", dict(
        playing=False, owes_fullscreen=False, swap_target=False,
        status=contains("App swap failed"), **FULL))

    app._toggle_pause()
    yield 3.0
    check("B2 resume, still refused: plays, the failed swap is not rerun",
          dict(playing=True, owes_fullscreen=False, **FULL))
    if results[-1]["app_osascript"]:
        results[-1]["ok"] = False
        results[-1]["wrong"]["app_osascript"] = "the resume ran a swap"
        print("         FAIL B2: the resume ran a swap", flush=True)

    app._toggle_pause()
    yield 4.0
    check("B3 next pause, still refused: tries again, fullscreen back", dict(
        playing=False, owes_fullscreen=False,
        status=contains("App swap failed"), **FULL))

    mode["refuse"] = False
    app._toggle_pause()
    yield 3.0
    check("B4 permission back, resume: plays fullscreen", dict(
        playing=True, **FULL))
    app._toggle_pause()
    yield 4.0
    check("B5 permission back, pause: the swap works again", dict(
        playing=False, owes_fullscreen=True, **WINDOWED, **STREAM_FRONT))
    app._toggle_pause()
    yield 5.0
    check("B6 and resume: fullscreen back, film in front", dict(
        playing=True, owes_fullscreen=False, **FULL, **FILM_FRONT))

    # --- C: External VLC and back --------------------------------------
    app._toggle_fullscreen()
    yield 3.0
    check("C0 fullscreen off by hand", dict(**WINDOWED))

    app.player_var.set("external")
    app._apply_player_choice()
    yield from until(lambda: "Loaded in external VLC" in status()
                     or status().startswith("External VLC:"), 25)
    yield 1.0
    ext = app.ctl.external
    check("C1 switch to External VLC: VLC comes up with the film", dict(
        embedded_playing=False, status=contains("Loaded in external VLC")),
        note=f"player is external: {app.ctl.player is ext}; "
             f"VLC answers: {ext._alive() if ext else None}")

    # a film opened meanwhile, and the film window closed meanwhile
    film2 = os.path.join(WORK, "second-film" + Path(FILM).suffix)
    os.symlink(FILM, film2)
    app.video_win.withdraw()
    app.file_lbl.config(text=Path(film2).name)
    app.ctl.load_file(film2)
    yield from until(lambda: Path(film2).name + " in external VLC" in status()
                     or status().startswith("External VLC:"), 20)
    yield 1.5
    check("C2 another film opened while external", dict(
        win_state="withdrawn", status=contains(Path(film2).name)))

    app.player_var.set("embedded")
    app._apply_player_choice()
    yield 1.5
    check("C3 back to built-in: film window shown", dict(
        win_state="normal", playing=False))
    if not (app.ctl.player is app.player_backend
            and app.ctl._embedded_path == film2
            and app.player_backend.has_media):
        results[-1]["ok"] = False
        results[-1]["wrong"]["film"] = "the built-in player was not given " \
                                       "the film opened meanwhile"
        print("         FAIL C3: the built-in player does not hold the "
              "film opened meanwhile", flush=True)

    started.clear()
    threading.Thread(target=start, daemon=True).start()
    yield from until(lambda: started, 10)
    yield 3.0
    check("C4 built-in plays it (what Sync does first)", dict(
        playing=True, has_vout=at_least_one, win_state="normal"),
        note=f"{started}; external VLC is now: "
             f"{(ext._status() or {}).get('state')!r}")

    app._toggle_fullscreen()
    yield 3.0
    check("C5 fullscreen still works after the switch", dict(
        playing=True, has_vout=at_least_one, **FULL))
    app._toggle_fullscreen()
    yield 3.0
    check("C6 and off again", dict(playing=True, **WINDOWED))

    # --- D: requests that land inside the fullscreen animation ----------
    app._toggle_fullscreen()
    yield 0.2
    app._toggle_fullscreen()
    yield 4.0
    check("D1 fullscreen on then off 0.2 s apart: ends windowed", dict(
        **WINDOWED))

    app._toggle_fullscreen()
    yield 3.0
    check("D2 fullscreen on", dict(**FULL))
    app._toggle_fullscreen()
    yield 0.2
    app._toggle_fullscreen()
    yield 4.0
    check("D3 fullscreen off then on 0.2 s apart: ends fullscreen", dict(
        **FULL))

    # (playing may go either way here: libvlc reports a pause late)
    app._toggle_pause()
    yield 0.15
    app._toggle_pause()
    yield 6.0
    check("D4 pause then resume 0.15 s apart: ends fullscreen, film in front",
          dict(owes_fullscreen=False, **FULL, **FILM_FRONT))
    app._toggle_fullscreen()
    yield 3.0
    check("D5 fullscreen off at the end", dict(**WINDOWED))


def finish():
    _stop_sampler.set()
    try:
        if app.fullscreen:
            app._set_fullscreen(False)
        app.player_backend.pause()
    except Exception:
        errors.append((now(), "cleanup", traceback.format_exc()[-800:]))
    ext = app.ctl.external
    if ext is not None and ext.proc is not None:
        ext.proc.terminate()               # the VLC this run started
    if helper_proc is not None:
        helper_proc.terminate()
    try:
        app._on_close()
    except Exception:
        pass

    bad = [r["step"] for r in results if not r["ok"]]
    if not results:
        bad = ["nothing ran"]
    print(f"\n{len(results) - len(bad)}/{len(results)} steps as expected; "
          f"{len(errors)} exceptions; {len(dialogs)} dialogs")
    for when, where, text in errors:
        print(f"--- exception ({where}, {when}s)\n{text}")
    for when, kind, title, message in dialogs:
        print(f"--- dialog ({kind}, {when}s): {title}: {message}")
    if bad or errors or dialogs:
        trace = os.path.join(tempfile.gettempdir(),
                             "streamsync-live-trace.json")
        with open(trace, "w") as f:
            json.dump({"results": results, "errors": errors,
                       "dialogs": dialogs, "calls": calls,
                       "timeline": timeline, "screen": [SW, SH]},
                      f, indent=1, default=str)
        print(f"every step's state, and the screen four times a second: "
              f"{trace}")
        print("MAC APP LIVE TEST FAILED")
    else:
        print("MAC APP LIVE TEST PASSED")
    shutil.rmtree(WORK, ignore_errors=True)
    sys.stdout.flush()
    # libvlc leaves native threads running that a plain exit would wait on
    os._exit(1 if bad or errors or dialogs else 0)


def run(gen):
    try:
        delay = next(gen)
    except StopIteration:
        finish()
        return
    except Exception:
        errors.append((now(), "script", traceback.format_exc()[-1500:]))
        finish()
        return
    root.after(int(delay * 1000), lambda: run(gen))


root.after(300000, finish)                 # never outstay five minutes
root.after(250, _tick)
root.after(500, lambda: run(script()))
try:
    root.mainloop()
finally:
    # Every way through the script leaves by finish(), which does not
    # return. Getting here means the run was stopped - the StreamSync
    # window closed, Cmd-Q, Ctrl-C - and the helper window and VLC would
    # otherwise be left behind.
    errors.append((now(), "script", "stopped before the script finished"))
    finish()
