"""Checks the diagnostics report says the right thing about VLC.

The point of this module is to replace a guess ("Install VLC from
videolan.org") with a fact, so what matters is that each distinct cause
produces its own answer. A report that is merely present, but names the
wrong cause, is worse than none - it sends someone to reinstall software
that was never the problem.

    python3 test_diagnostics.py
"""

import ast
import io
import logging
import os
import platform
import sys
import tempfile
import threading
import types

import diagnostics

fails = []


def check(label, got, want):
    if got != want:
        fails.append(f"{label}: got {got!r}, wanted {want!r}")


def ok(label, cond):
    if not cond:
        fails.append(label)


def probe(candidates, **kw):
    """A probe_vlc() result, without needing VLC or a Mac."""
    info = {"candidates": candidates, "loadable": None, "import_error": None,
            "libvlc_version": None, "plugins": None, "depcheck_found": None,
            "instance": None, "plugin_path": None}
    info.update(kw)
    return info


# --- each cause gets its own answer -------------------------------------
absent = probe([{"path": "/Applications/VLC.app/x", "exists": False}])
ok("absent VLC is reported as not installed",
   "does not appear to be installed" in diagnostics.vlc_summary(absent))

# The case that reads identically to "not installed" in the old message,
# and has the opposite fix: VLC is there, it is simply the wrong build.
wrong_arch = probe([{
    "path": "/Applications/VLC.app/x", "exists": True, "loads": False,
    "load_error": ("dlopen(...): tried: '/Applications/VLC.app/x' (mach-o "
                   "file, but is an incompatible architecture (have "
                   "'x86_64', need 'arm64e'))")}])
summary = diagnostics.vlc_summary(wrong_arch)
ok("an architecture mismatch is named as one",
   "different processor" in summary)
ok("and it does not tell them to install what they already have",
   "does not appear to be installed" not in summary)

signature = probe([{
    "path": "/Applications/VLC.app/x", "exists": True, "loads": False,
    "load_error": "code signature not valid for use in process"}])
ok("a code-signing refusal is named as one",
   "code-signing" in diagnostics.vlc_summary(signature))

# Present, loadable, but no plugins: libvlc gives back a null instance
# rather than an error, so nothing else in the app can explain this.
noplugins = probe(
    [{"path": "/Applications/VLC.app/x", "exists": True, "loads": True}],
    loadable="/Applications/VLC.app/x",
    plugins="missing: /Applications/VLC.app/Contents/MacOS/plugins")
ok("missing plugins are named",
   "plugins folder is missing" in diagnostics.vlc_summary(noplugins))

working = probe(
    [{"path": "/Applications/VLC.app/x", "exists": True, "loads": True}],
    loadable="/Applications/VLC.app/x", plugins="/x (400 plugins)")
check("a loadable VLC is reported as loading",
      diagnostics.vlc_summary(working), "VLC loads correctly here.")

# The case the first field report got wrong: python-vlc imported and read
# a version out of the real library, and the verdict still said VLC would
# not load - because a shallower probe (a bare dlopen) had failed. Deeper
# evidence must always win over shallower.
field_report = probe(
    [{"path": "/Applications/VLC.app/x", "exists": True, "loads": False,
      "load_error": "Failed to load dynlib/dll ... frozen."}],
    libvlc_version="3.0.23 Vetinari", instance="ok",
    plugins="/x (400 plugins)")
fr = diagnostics.vlc_summary(field_report)
ok("a working instance outranks a failed load probe", "VLC works here" in fr)
ok("and points the search past VLC itself", "after startup" in fr)

# Library loads, instance does not: the plugins diagnosis, reached from
# real evidence this time rather than from the load probe.
no_instance = probe(
    [{"path": "/Applications/VLC.app/x", "exists": True, "loads": True}],
    libvlc_version="3.0.23 Vetinari",
    instance="libvlc returned no instance",
    plugins="missing: /Applications/VLC.app/Contents/MacOS/plugins")
ni = diagnostics.vlc_summary(no_instance)
ok("a version without an instance names the instance failure",
   "no player could be started" in ni)
ok("and names the plugins folder", "plugins" in ni)

# VLC itself fine, bindings broken - a different fix from all of the above.
bindings = probe(
    [{"path": "/Applications/VLC.app/x", "exists": True, "loads": True}],
    loadable="/Applications/VLC.app/x", plugins="/x (400 plugins)",
    import_error="ModuleNotFoundError: No module named 'vlc'")
ok("a bindings-only failure is not blamed on VLC",
   "python-vlc bindings" in diagnostics.vlc_summary(bindings))

# An unrecognised dlopen failure must still carry what dlopen said,
# rather than falling through to a cheerful default.
odd = probe([{"path": "/x", "exists": True, "loads": False,
              "load_error": "something nobody has seen before"}])
odd_summary = diagnostics.vlc_summary(odd)
ok("an unknown load failure repeats the system's own words",
   "something nobody has seen before" in odd_summary)
ok("and is not reported as success", "loads correctly" not in odd_summary)

# --- the report itself ---------------------------------------------------
text = diagnostics.report()
for section in ("StreamSync diagnostics", "VLC", "Video output", "Network",
                "First-run gate", "verdict:"):
    ok(f"report contains {section!r}", section in text)
ok("report names the log file", diagnostics.LOG_FILE in text)

info = diagnostics.describe()
for key in ("streamsync", "frozen", "process_arch", "translocated", "log"):
    ok(f"describe() reports {key}", key in info)

# --- the log -------------------------------------------------------------
saved_dir, saved_file = diagnostics.LOG_DIR, diagnostics.LOG_FILE
tmp = tempfile.mkdtemp()
try:
    diagnostics.LOG_DIR = tmp
    diagnostics.LOG_FILE = os.path.join(tmp, "streamsync.log")

    diagnostics.log("hello")
    with open(diagnostics.LOG_FILE) as f:
        ok("log writes the message", "hello" in f.read())

    diagnostics.log_block("block:", "one\ntwo")
    with open(diagnostics.LOG_FILE) as f:
        body = f.read()
    ok("log_block indents every line",
       "    one" in body and "    two" in body)

    # A log that grows without bound is a bug report nobody can send.
    with open(diagnostics.LOG_FILE, "w") as f:
        f.write("x" * (diagnostics.MAX_LOG_BYTES + 1))
    diagnostics.log("after rotation")
    ok("an oversized log is rotated aside",
       os.path.exists(diagnostics.LOG_FILE + ".1"))
    ok("and the live log restarts small",
       os.path.getsize(diagnostics.LOG_FILE) < 1000)

    # The whole point is evidence surviving a crash nobody watched.
    saved_hook, saved_thread_hook = sys.excepthook, threading.excepthook
    try:
        # Silence the hooks we chain to, so a passing run prints no
        # traceback and a real failure is the only thing on screen.
        chained, thread_chained = [], []
        sys.excepthook = lambda *a: chained.append(a)
        threading.excepthook = thread_chained.append
        diagnostics.install_excepthook()
        # streamsync.py installs the hooks, then the shell again: that
        # must not log every crash twice.
        diagnostics.install_excepthook()
        inner = sys.excepthook
        try:
            raise ValueError("boom")
        except ValueError:
            inner(*sys.exc_info())
        with open(diagnostics.LOG_FILE) as f:
            body = f.read()
        ok("an uncaught exception reaches the log",
           "UNCAUGHT EXCEPTION" in body and "boom" in body)
        ok("and is still passed on to the previous hook", len(chained) == 1)
        check("installing the hooks twice logs a crash once",
              body.count("ValueError: boom"), 1)

        # Where the app's crashes actually happen: listening, matching
        # and seeking all run on worker threads, whose exceptions never
        # reach sys.excepthook.
        def listen():
            raise RuntimeError("worker boom")

        worker = threading.Thread(target=listen, name="audio-worker")
        worker.start()
        worker.join()
        with open(diagnostics.LOG_FILE) as f:
            body = f.read()
        ok("a worker thread's exception reaches the log",
           "RuntimeError: worker boom" in body)
        ok("with its full traceback",
           "Traceback (most recent call last)" in body
           and "in listen" in body)
        ok("naming the thread it killed", "'audio-worker'" in body)
        check("and is still passed on to the previous thread hook",
              [a.exc_type for a in thread_chained], [RuntimeError])

        # A thread quitting on purpose is not a crash.
        quitter = threading.Thread(target=sys.exit, name="quitter")
        quitter.start()
        quitter.join()
        with open(diagnostics.LOG_FILE) as f:
            ok("a thread's SystemExit is not logged as a crash",
               "'quitter'" not in f.read())
    finally:
        sys.excepthook, threading.excepthook = saved_hook, saved_thread_hook

    # A Tk callback's exception is caught by Tk itself and printed to a
    # stderr the windowed build does not have - the button just does
    # nothing. Driven through tkinter's own callback wrapper on a root
    # that never opens a window.
    def on_click():
        raise KeyError("tk boom")

    try:
        import tkinter
    except ImportError:          # a Python built without Tk: call it as Tk would
        tk_root = types.SimpleNamespace()
        diagnostics.install_tk_hook(tk_root)
        try:
            on_click()
        except KeyError:
            tk_root.report_callback_exception(*sys.exc_info())
    else:
        tk_root = tkinter.Tk.__new__(tkinter.Tk)
        tk_root.master = None
        diagnostics.install_tk_hook(tk_root)
        tkinter.CallWrapper(on_click, None, tk_root)()
    with open(diagnostics.LOG_FILE) as f:
        body = f.read()
    ok("a Tk callback's exception reaches the log",
       "UNCAUGHT EXCEPTION in a Tk callback" in body
       and "KeyError: 'tk boom'" in body)
    ok("with its full traceback", "in on_click" in body)

    # Python logging lands in the same file (install_excepthook, above,
    # routed it there): a module that wants levels and exc_info gets
    # them without a second log nobody knows to look for.
    logger = logging.getLogger("streamsync.test")
    saved_err, sys.stderr = sys.stderr, io.StringIO()
    try:
        logger.info("film loaded")
        logger.debug("frame detail")
        try:
            raise OSError("disk gone")
        except OSError:
            logger.warning("save FAILED", exc_info=True)
        echoed = sys.stderr.getvalue()
    finally:
        sys.stderr = saved_err
    with open(diagnostics.LOG_FILE) as f:
        body = f.read()
    ok("a module logger's line reaches the log",
       "INFO streamsync.test: film loaded" in body)
    check("once, though the handler was installed twice",
          body.count("film loaded"), 1)
    ok("with exc_info's traceback, indented as one entry",
       "WARNING streamsync.test: save FAILED\n    Traceback" in body
       and "\n    OSError: disk gone" in body)
    ok("DEBUG detail reaches the file",
       "DEBUG streamsync.test: frame detail" in body)
    ok("but not the Terminal", "frame detail" not in echoed)
    ok("where INFO still echoes, as log() does", "film loaded" in echoed)

    # More detail needs more room, and still a bound: three old logs of
    # up to 2 MB beside the live one, the oldest dropped. Through the
    # handler, which writes with log()'s own rotation.
    for gen in range(1, 6):
        with open(diagnostics.LOG_FILE, "w") as f:
            f.write(f"generation {gen}\n" + "x" * diagnostics.MAX_LOG_BYTES)
        logger.info("rotate")
    check("rotation keeps three old logs",
          sorted(n for n in os.listdir(tmp) if n != "streamsync.log"),
          ["streamsync.log.1", "streamsync.log.2", "streamsync.log.3"])

    def generation(path):
        try:
            with open(path) as f:
                return f.readline(40).strip()
        except OSError:
            return None

    check("the newest is .1", generation(diagnostics.LOG_FILE + ".1"),
          "generation 5")
    check("the oldest kept is .3", generation(diagnostics.LOG_FILE + ".3"),
          "generation 3")
    ok("the live log restarts with the line that rotated it",
       os.path.getsize(diagnostics.LOG_FILE) < 1000)
    ok("each log may reach 2 MB", diagnostics.MAX_LOG_BYTES >= 2_000_000)

    # Every run's first line says what it ran on - on Windows too, where
    # describe() has no OS version to offer.
    diagnostics.log_session_start()
    with open(diagnostics.LOG_FILE) as f:
        start = [ln for ln in f if " starting (" in ln]
    ok("the startup line is logged", len(start) == 1)
    ok("and names the OS and its build",
       bool(start) and platform.platform() in start[0])
    ok("and how the app was launched",
       bool(start) and f"args {sys.argv[1:]}" in start[0])

    # A read-only log directory must not take the app down with it.
    diagnostics.LOG_FILE = "/nonexistent/nowhere/streamsync.log"
    diagnostics.LOG_DIR = "/nonexistent/nowhere"
    try:
        diagnostics.log("this cannot be written")
    except Exception as e:
        fails.append(f"log raised when it could not write: {e!r}")
finally:
    diagnostics.LOG_DIR, diagnostics.LOG_FILE = saved_dir, saved_file

# --- both shells install the hooks ---------------------------------------
# A shell can be launched directly (`python app.py`, and CI's --selftest)
# without streamsync.py, and only a shell holds the Tk root. Read rather
# than run: main() opens the real window.
for shell in ("app.py", "mac_app.py"):
    tree = ast.parse(open(shell, encoding="utf-8").read(), shell)
    main_fn = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    called = {f"{n.func.value.id}.{n.func.attr}" for n in ast.walk(main_fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and isinstance(n.func.value, ast.Name)}
    for hook in ("diagnostics.install_excepthook",
                 "diagnostics.install_tk_hook"):
        ok(f"{shell} main() calls {hook}", hook in called)

# --- probing must never be what breaks the app ---------------------------
# It runs while diagnosing an already-broken machine, so every part of it
# has to tolerate the tools it calls being missing or hostile.
saved_run = diagnostics._run
try:
    diagnostics._run = lambda *a, **k: None
    live = diagnostics.probe_vlc()
    ok("probe_vlc survives every subprocess failing", isinstance(live, dict))
    ok("and still answers", bool(diagnostics.vlc_summary(live)))
finally:
    diagnostics._run = saved_run

# --- video embedding: the fix for sound-with-no-picture ------------------
# The first Mac field test ran a whole session - launch, sync, "successful
# match" - with playback into a window that did not exist, because libvlc's
# macOS output renders nothing until handed an NSView. attach_tk is that
# handoff; these pin its contract without needing a Mac or VLC.
import players

if sys.platform != "darwin":
    ok("the NSView bridge answers None where the symbol cannot exist",
       players._tk_nsview(0) is None)

_calls = []
_fake = types.SimpleNamespace(
    embedded=False,
    mp=types.SimpleNamespace(set_nsobject=_calls.append))

_saved_nsview = players._tk_nsview
try:
    players._tk_nsview = lambda wid: None
    ok("no NSView means no embedding, reported honestly",
       players.EmbeddedPlayer.attach_tk(_fake, 42) is False)
    ok("and the player is not marked embedded", _fake.embedded is False)
    check("and libvlc was never handed a bogus view", _calls, [])

    players._tk_nsview = lambda wid: 0xD0A
    ok("a real NSView is handed to libvlc",
       players.EmbeddedPlayer.attach_tk(_fake, 42) is True)
    check("as the view the bridge resolved", _calls, [0xD0A])
    ok("and the player is marked embedded", _fake.embedded is True)

    _calls.clear()
    ok("attaching twice is a cheap no-op",
       players.EmbeddedPlayer.attach_tk(_fake, 42) is True)
    check("that does not touch libvlc again", _calls, [])
finally:
    players._tk_nsview = _saved_nsview

# --- a module used but never imported ------------------------------------
# The diagnostics window called subprocess.run in a file that never
# imported subprocess: fine until someone clicks the button, then a
# NameError. Nothing else here would have caught that.
STDLIB = {"subprocess", "os", "sys", "json", "time", "threading", "tempfile",
          "queue", "platform", "ctypes", "traceback", "shutil", "webbrowser",
          "math", "base64", "ssl", "socket", "hashlib", "plistlib", "shlex"}

for name in sorted(f for f in os.listdir(".") if f.endswith(".py")):
    tree = ast.parse(open(name, encoding="utf-8").read(), name)
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            bound |= {(a.asname or a.name) for a in node.names}
        elif isinstance(node, (ast.Assign, ast.For, ast.withitem,
                               ast.FunctionDef, ast.arg)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    bound.add(sub.id)
                elif isinstance(sub, ast.arg):
                    bound.add(sub.arg)

    used = {n.value.id for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    missing = sorted((used & STDLIB) - bound)
    if missing:
        fails.append(f"{name} uses {', '.join(missing)} without importing it")

if fails:
    print("DIAGNOSTICS TEST FAILED")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("DIAGNOSTICS TEST PASSED")
