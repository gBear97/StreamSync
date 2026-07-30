"""One-call logging setup: a rotating debug log at ~/.streamsync.log.

Standard library only, so it can load before the dependency gate has
checked for any package. The status bar forgets what it said; this file
is the answer to "what did the app actually do" after a field run -
size-capped so it can never eat a disk.
"""
import logging
import logging.handlers
import platform
import sys
import threading
from pathlib import Path

LOG_PATH = Path.home() / ".streamsync.log"


def setup():
    root = logging.getLogger("streamsync")
    if root.handlers:                      # second call is a no-op
        return root
    root.setLevel(logging.DEBUG)
    fh = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname).1s %(name)s: %(message)s"))
    root.addHandler(fh)
    if sys.stderr is not None:             # no console in --windowed builds
        sh = logging.StreamHandler()
        sh.setLevel(logging.WARNING)
        root.addHandler(sh)

    def thread_hook(args, _prev=threading.excepthook):
        root.error("unhandled exception in thread %r",
                   args.thread.name if args.thread else "?",
                   exc_info=(args.exc_type, args.exc_value,
                             args.exc_traceback))
        _prev(args)

    threading.excepthook = thread_hook
    sys.excepthook = lambda *exc: root.error("unhandled exception",
                                             exc_info=exc)
    root.info("--- start: frozen=%s python=%s %s argv=%s ---",
              getattr(sys, "frozen", False), sys.version.split()[0],
              platform.platform(terse=True), sys.argv)
    return root
