"""StreamSync entry point: run the dependency gate, then the app.

The gate uses only the standard library, so this starts cleanly even on a
machine with no packages installed yet. Windows and macOS get their own
UI shells on top of the same sync engine.
"""

import os
import sys
import traceback

import depcheck

ERROR_LOG = os.path.expanduser("~/.streamsync-error.log")


def _report(details):
    """Surface a startup crash: a --windowed build has no console to print to."""
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
