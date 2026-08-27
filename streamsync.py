"""StreamSync entry point: run the dependency gate, then the app.

The gate uses only the standard library, so this starts cleanly even on a
machine with no packages installed yet. Windows and macOS get their own
UI shells on top of the same sync engine.
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

    try:
        ready = depcheck.ensure_ready()
    except Exception:
        _report(traceback.format_exc())
        raise
    if not ready:
        return
    if sys.platform == "darwin":
        import mac_app as app
    else:
        import app
    app.main()


if __name__ == "__main__":
    main()
