"""What StreamSync knows about its own environment, written down.

A shipped .app has no console. When it fails on someone else's Mac the
only evidence is a dialog they have to retype back to us, and that dialog
names the likeliest cause rather than the actual one: "Install VLC from
videolan.org" reads identically whether VLC is missing, is the wrong
architecture for this build, is a version whose ABI we cannot speak, or
is perfectly fine and something else entirely broke.

So this writes a log where macOS keeps logs, and can produce a report
answering the question a screenshot cannot: which libvlc was found,
whether this process can actually load it, and what the operating system
said when it could not.

    StreamSync.app/Contents/MacOS/StreamSync --diagnose

prints that report and exits. The same report is written to the log
whenever the player fails to start, so the evidence exists even when
nobody thought to ask for it first.

Stdlib only, so the dependency gate can use it before anything is
installed - and so it keeps working in the situation it exists for,
where the environment is already broken.
"""

import ctypes
import os
import platform
import subprocess
import sys
import time
import traceback

IS_MAC = sys.platform == "darwin"

# Where a Mac user (and Console.app) expects to find an app's log.
if IS_MAC:
    LOG_DIR = os.path.expanduser("~/Library/Logs/StreamSync")
else:
    LOG_DIR = os.path.expanduser("~/.streamsync")
LOG_FILE = os.path.join(LOG_DIR, "streamsync.log")

MAX_LOG_BYTES = 1_000_000

VLC_DYLIB_CANDIDATES = [
    "/Applications/VLC.app/Contents/MacOS/lib/libvlc.dylib",
    os.path.expanduser("~/Applications/VLC.app/Contents/MacOS/lib/libvlc.dylib"),
]


def _run(cmd, timeout=10):
    """Best-effort command output. Never raises - this runs while
    diagnosing a broken machine, where anything may be unavailable."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (r.stdout or "") + (r.stderr or "")
    return out.strip() or None


# --- the log ------------------------------------------------------------

def _rotate():
    try:
        if os.path.getsize(LOG_FILE) < MAX_LOG_BYTES:
            return
    except OSError:
        return
    try:
        os.replace(LOG_FILE, LOG_FILE + ".1")
    except OSError:
        pass


def log(message):
    """Append one timestamped line. Also to stderr, which a `open -a`
    launch discards but a Terminal launch shows."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp}  {message}"
    try:
        sys.stderr.write(line + "\n")
    except Exception:
        pass
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        _rotate()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # a log we cannot write is not worth crashing over


def log_block(title, text):
    """Log a multi-line block, indented so it reads as one entry."""
    body = "\n".join("    " + ln for ln in str(text).splitlines())
    log(f"{title}\n{body}")


# --- what this build is -------------------------------------------------

def _version():
    try:
        import version
        return getattr(version, "__version__", "unknown")
    except Exception:
        return "unknown"


def _translocated():
    """macOS runs a quarantined app from a read-only shadow copy until it
    is moved out of the disk image. Paths look wrong in ways that are
    nobody's bug, so it is worth naming rather than puzzling over."""
    return "/AppTranslocation/" in os.path.realpath(sys.executable)


def describe():
    return {
        "streamsync": _version(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "translocated": _translocated(),
        "process_arch": platform.machine(),
        "python": platform.python_version(),
        "macos": platform.mac_ver()[0] or "n/a",
        "log": LOG_FILE,
    }


# --- the VLC question ---------------------------------------------------

def _spotlight_vlc():
    out = _run(["mdfind",
                "kMDItemCFBundleIdentifier == 'org.videolan.vlc'"], timeout=15)
    if not out:
        return []
    return [os.path.join(p.strip(), "Contents/MacOS/lib/libvlc.dylib")
            for p in out.splitlines() if p.strip()]


def _arch_of(path):
    out = _run(["/usr/bin/file", "-b", path])
    return out.splitlines()[0] if out else None


def _signer_of(path):
    out = _run(["/usr/bin/codesign", "-dvv", path])
    if not out:
        return None
    for line in out.splitlines():
        if line.startswith("Authority="):
            return line.split("=", 1)[1]
    return None


def _dlopen_error(path):
    """dyld's own reason a library will not load, from dlerror().

    Needed because a frozen app's ctypes rewrites every load failure into
    "Most likely this dynlib/dll was not found when the application was
    frozen" - a guess, printed in place of the actual reason, which is
    the exact disease this module exists to cure."""
    try:
        libsys = ctypes.CDLL(None)
        libsys.dlopen.restype = ctypes.c_void_p
        libsys.dlopen.argtypes = [ctypes.c_char_p, ctypes.c_int]
        libsys.dlerror.restype = ctypes.c_char_p
        libsys.dlclose.argtypes = [ctypes.c_void_p]
        libsys.dlerror()  # clear any stale message
        handle = libsys.dlopen(path.encode(), 0x2)  # RTLD_NOW
        if handle:
            libsys.dlclose(handle)
            return None
        err = libsys.dlerror()
        return err.decode() if err else "dlopen failed with no message"
    except Exception as e:
        return f"(could not ask dyld: {e})"


def _try_load(path):
    """Whether this process can load the library the way the app does.

    python-vlc pre-loads libvlccore before libvlc, because libvlc refers
    to it in a form dyld can only satisfy once it is already in the
    process - so a bare load of libvlc alone fails even on a perfectly
    healthy machine, which is precisely the false alarm the first field
    report of this module produced. Load it the same way the app will.
    """
    core = os.path.join(os.path.dirname(path), "libvlccore.dylib")
    if os.path.isfile(core):
        try:
            ctypes.CDLL(core)
        except Exception:
            pass  # let libvlc's own failure tell the story
    try:
        ctypes.CDLL(path)
        return True, None
    except Exception as e:
        return False, _dlopen_error(path) or str(e)


def probe_vlc():
    """Everything that bears on whether libvlc will load here."""
    info = {"candidates": [], "loadable": None, "import_error": None,
            "libvlc_version": None, "plugins": None, "depcheck_found": None,
            "instance": None, "plugin_path": None}

    seen = set()
    for path in VLC_DYLIB_CANDIDATES + (_spotlight_vlc() if IS_MAC else []):
        if path in seen:
            continue
        seen.add(path)
        entry = {"path": path, "exists": os.path.isfile(path)}
        if entry["exists"]:
            entry["arch"] = _arch_of(path)
            entry["signed_by"] = _signer_of(path)
            ok, err = _try_load(path)
            entry["loads"] = ok
            entry["load_error"] = err
            if ok and info["loadable"] is None:
                info["loadable"] = path
        info["candidates"].append(entry)

    # What the gate concluded, which is what decides whether the user was
    # shown an installer prompt. If it disagrees with "loads", that gap is
    # the bug.
    try:
        import depcheck
        info["depcheck_found"] = depcheck.vlc_dll_path()
    except Exception as e:
        info["depcheck_found"] = f"(depcheck failed: {e})"

    # python-vlc's own import, with the traceback rather than a summary:
    # it fails for several unrelated reasons that read alike condensed.
    try:
        import vlc
        try:
            info["libvlc_version"] = vlc.libvlc_get_version().decode()
        except Exception as e:
            info["libvlc_version"] = f"(unavailable: {e})"
        info["plugin_path"] = getattr(vlc, "plugin_path", None)
    except Exception:
        info["import_error"] = traceback.format_exc()

    # The report that prompted this module's first fix imported vlc and
    # read a version out of the real library while the app still had no
    # player - so loading is not the last question. Make the exact calls
    # the app makes and record where they stop.
    if not info["import_error"]:
        try:
            inst = vlc.Instance("--no-video-title-show", "--quiet")
            if inst is None:
                info["instance"] = "libvlc returned no instance"
            else:
                mp = inst.media_player_new()
                info["instance"] = ("ok" if mp is not None
                                    else "instance ok, but no media player")
                for h in (mp, inst):
                    try:
                        h.release()
                    except Exception:
                        pass
        except Exception as e:
            info["instance"] = f"failed: {e!r}"

    # libvlc without its plugins loads fine and then cannot build an
    # instance, which surfaces later as a different, vaguer error - so
    # locate them from any installed VLC, loadable or not.
    installed = info["loadable"] or next(
        (c["path"] for c in info["candidates"] if c.get("exists")), None)
    if installed:
        base = installed.split("/Contents/MacOS/")[0]
        pdir = os.path.join(base, "Contents/MacOS/plugins")
        if os.path.isdir(pdir):
            try:
                n = len([f for f in os.listdir(pdir) if f.endswith(".dylib")])
            except OSError:
                n = "?"
            info["plugins"] = f"{pdir} ({n} plugins)"
        else:
            info["plugins"] = f"missing: {pdir}"

    return info


def vlc_summary(info=None):
    """One sentence naming the actual cause, for a dialog that has room
    for exactly one sentence.

    Ranked by depth of evidence: an instance actually starting beats the
    import working, which beats a load probe, which beats file existence.
    The first field report of this module got that order wrong and
    declared VLC unloadable two lines above the version string it had
    just read out of the loaded library.
    """
    info = probe_vlc() if info is None else info
    present = [c for c in info["candidates"] if c.get("exists")]

    # python-vlc imported and spoke to the real library - the level the
    # app actually uses it at.
    if not info.get("import_error") and info.get("libvlc_version"):
        if info.get("instance") == "ok":
            return ("VLC works here: its library loads and a player "
                    "instance starts. A failure in the app is now after "
                    "startup, not in loading VLC.")
        if info.get("instance"):
            detail = info.get("plugins") or "plugins were not located"
            return ("VLC's library loads, but no player could be started "
                    f"({info['instance']}). That is nearly always its "
                    f"plugins folder: {detail}.")
        return "VLC's library loads here."

    if info.get("loadable"):
        if info.get("plugins") and info["plugins"].startswith("missing"):
            return ("VLC was found and loads, but its plugins folder is "
                    "missing, so no media can be opened.")
        if info.get("import_error"):
            return ("VLC itself loads here, so the failure is in the "
                    "python-vlc bindings rather than in VLC.")
        return "VLC loads correctly here."

    if not present:
        return ("VLC does not appear to be installed - no libvlc.dylib "
                "was found in /Applications or anywhere Spotlight knows.")

    # Present but unloadable is the interesting case, and dlopen has
    # already said why - so repeat what it said rather than guessing.
    err = next((c.get("load_error") for c in present if c.get("load_error")),
               "") or ""
    if "incompatible architecture" in err:
        arch = platform.machine()
        return (f"The installed VLC is built for a different processor "
                f"than this copy of StreamSync ({arch}). Install the VLC "
                f"matching your Mac, or the StreamSync build matching VLC.")
    if "code signature" in err or "Library Validation" in err:
        return ("macOS refused to let StreamSync load VLC's library for "
                "code-signing reasons.")
    return f"VLC is installed but its library will not load here: {err}"


# --- the report ---------------------------------------------------------

def report():
    lines = ["StreamSync diagnostics", "=" * 60, ""]

    for k, v in describe().items():
        lines.append(f"  {k}: {v}")

    lines += ["", "VLC", "-" * 60]
    info = probe_vlc()
    lines.append(f"  verdict: {vlc_summary(info)}")
    lines.append(f"  depcheck found: {info['depcheck_found']}")
    lines.append(f"  libvlc version: {info['libvlc_version']}")
    lines.append(f"  player instance: {info['instance']}")
    lines.append(f"  plugin path (python-vlc): {info['plugin_path']}")
    lines.append(f"  plugins: {info['plugins']}")
    lines.append("")

    for c in info["candidates"]:
        if not c.get("exists"):
            lines.append(f"  [absent] {c['path']}")
            continue
        lines.append(f"  [found]  {c['path']}")
        lines.append(f"             arch: {c.get('arch')}")
        lines.append(f"             signed by: {c.get('signed_by')}")
        lines.append(f"             loads: {c.get('loads')}")
        if c.get("load_error"):
            lines.append(f"             error: {c['load_error']}")

    if info["import_error"]:
        lines += ["", "  import vlc raised:"]
        lines += ["    " + ln for ln in info["import_error"].splitlines()]

    # The gate's own list, so this can never drift into probing packages
    # the app does not use - the first field report flagged sounddevice
    # as MISSING, a module this app never imports.
    lines += ["", "Bundled packages", "-" * 60]
    try:
        from depcheck import REQUIRED_PKGS
        needed = [imp for imp, _pip in REQUIRED_PKGS if imp != "vlc"]
    except Exception:
        needed = ["numpy", "PIL", "mss", "imageio_ffmpeg", "soundcard"]
    for mod in needed:
        try:
            __import__(mod)
            lines.append(f"  {mod}: present")
        except Exception as e:
            lines.append(f"  {mod}: MISSING ({type(e).__name__}: {e})")

    # The video window's one precondition. The first Mac field test had
    # audio, a match, and no picture, because libvlc was never handed an
    # NSView - and the bridge that supplies one is this single symbol
    # from Tk's own library.
    lines += ["", "Video output", "-" * 60]
    try:
        import tkinter  # noqa: F401 - loads Tk's dylib into the process
        has = hasattr(ctypes.CDLL(None), "TkMacOSXGetRootControl")
    except Exception:
        has = False
    lines.append("  Tk NSView bridge (TkMacOSXGetRootControl): "
                 + ("available" if has else "not found"))

    # What the first-run gate will decide, since a blocked gate and a
    # crashed app look identical from the outside: no app either way.
    lines += ["", "First-run gate", "-" * 60]
    try:
        import depcheck
        c = depcheck.collect()
        for k in sorted(c):
            lines.append(f"  {k}: {c[k]}")
        lines.append(f"  gate lets the app start: {depcheck.all_ok(c)}")
    except Exception as e:
        lines.append(f"  gate unavailable: {e!r}")

    lines += ["", "Network", "-" * 60]
    try:
        import netcerts
        for k, v in netcerts.describe().items():
            lines.append(f"  {k}: {v}")
    except Exception as e:
        lines.append(f"  netcerts unavailable: {e}")

    return "\n".join(lines)


def log_report(reason=""):
    """Write the full report to the log. Called when something fails, so
    the evidence is on disk before anyone thinks to go looking."""
    if reason:
        log(f"--- diagnostics ({reason}) ---")
    log_block("report:", report())
    return LOG_FILE


# --- crashes ------------------------------------------------------------

def install_excepthook():
    """An uncaught exception in a windowed build vanishes silently. This
    is the only reason such a crash leaves any trace at all."""
    previous = sys.excepthook

    def hook(exc_type, exc, tb):
        try:
            log_block("UNCAUGHT EXCEPTION:",
                      "".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:
            pass
        previous(exc_type, exc, tb)

    sys.excepthook = hook


def log_session_start():
    log("=" * 60)
    d = describe()
    log(f"StreamSync {d['streamsync']} starting "
        f"({d['process_arch']}, frozen={d['frozen']}, python {d['python']})")
    if d["translocated"]:
        log("NOTE: running translocated - the app has not been moved out "
            "of the disk image, so macOS is running a shadow copy.")


if __name__ == "__main__":
    print(report())
