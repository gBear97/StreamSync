"""StreamSync entry point: run the dependency gate, then the app.

The gate uses only the standard library, so this starts cleanly even on a
machine with no packages installed yet. Then the window (ui.shell), the
same one on Windows and macOS, with each platform's own parts in
ui.platform_win and ui.platform_mac.

    python streamsync.py              the app
    python streamsync.py --selftest   open, tour the dialogs and close, with
                                      a throwaway config; exit 0 when it
                                      worked, 1 when a window or dialog
                                      raised, 2 with no VLC, 3 when the
                                      gate would have stopped to ask
    python streamsync.py --diagnose   the diagnostics report, printed
    python streamsync.py --netcheck   can this build make a verified HTTPS
                                      request?
"""

import os
import sys
import traceback

import depcheck
import diagnostics

ERROR_LOG = os.path.expanduser("~/.streamsync-error.log")


def _report(details):
    """Surface a startup crash: a --windowed build has no console to print to."""
    diagnostics.log_block("STARTUP FAILURE:", details)
    try:
        with open(ERROR_LOG, "w") as f:
            f.write(details)
        where = ERROR_LOG
    except OSError:
        where = None
    sys.stderr.write(details)
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror(
            "StreamSync failed to start",
            details.strip().splitlines()[-1] +
            (f"\n\nFull details: {where}" if where else ""))
        root.destroy()
    except Exception:
        pass  # no Tk, or Tk is itself the casualty - the log still has it


def main():
    # A frozen build has no console and no test harness reaching inside it,
    # so this is how CI asks the shipped bundle - the real one, not a
    # source checkout with the system's CA store behind it - whether it can
    # actually complete a verified HTTPS request.
    if "--netcheck" in sys.argv:
        import netcerts
        raise SystemExit(netcerts.selfcheck())

    # The same question the app answers badly from inside a dialog, asked
    # from a Terminal where the whole answer fits.
    if "--diagnose" in sys.argv:
        print(diagnostics.report())
        raise SystemExit(0)

    # Before anything else can fail: a windowed build has no console, so
    # without this an uncaught exception leaves nothing behind at all.
    diagnostics.install_excepthook()
    diagnostics.log_session_start()

    # An unattended run must never sit on the gate's dialog, waiting for a
    # click nobody will make: what it would have asked about is printed
    # instead, and the run fails.
    if "--selftest" in sys.argv:
        checks = depcheck.collect()
        if not depcheck.all_ok(checks):
            print(f"SELFTEST: not ready - {_unmet(checks)}", file=sys.stderr)
            raise SystemExit(3)

    try:
        ready = depcheck.ensure_ready()
    except Exception:
        _report(traceback.format_exc())
        raise
    if not ready:
        return
    from ui import shell
    shell.main()


def _unmet(checks):
    """What depcheck.all_ok found missing, in a line."""
    out = []
    if checks["packages"]:
        out.append("packages missing: " + ", ".join(checks["packages"]))
    if checks["vlc"] is None:
        out.append("VLC not found")
    if checks["ffmpeg"] is False:          # None: not tried, for want of packages
        out.append("ffmpeg does not run")
    return "; ".join(out)


if __name__ == "__main__":
    main()
