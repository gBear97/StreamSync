"""StreamSync entry point: run the dependency gate, then the app.

The gate uses only the standard library, so this starts cleanly even on a
machine with no packages installed yet. Windows and macOS get their own
UI shells on top of the same sync engine.
"""

import sys

import depcheck

if depcheck.ensure_ready():
    if sys.platform == "darwin":
        import mac_app as app
    else:
        import app
    app.main()
