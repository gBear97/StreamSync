#!/bin/sh
# Build StreamSync.app - run this ON a Mac, from this folder.
# VLC.app must still be installed on the machine that runs it.
#
# The interpreter you build with decides which Tk gets bundled, and the
# app will not run on the Tk 8.5 that Apple's /usr/bin/python3 ships. Set
# PYTHON to the one you actually run StreamSync with:
#
#     PYTHON=.venv/bin/python sh build_app_mac.sh
#
set -e
PY="${PYTHON:-python3}"

# Fail here rather than shipping a bundle that dies on launch.
"$PY" - <<'PYCHECK'
import sys
try:
    import tkinter
except ImportError:
    sys.exit("This Python has no tkinter - see MAC_FIRST_RUN.md step 0.")
if tkinter.TkVersion < 8.6:
    sys.exit(f"Tk {tkinter.TkVersion} is too old for StreamSync's UI. Build "
             "with a python.org or Homebrew Python (MAC_FIRST_RUN.md step 0), "
             "e.g. PYTHON=.venv/bin/python sh build_app_mac.sh")
PYCHECK

"$PY" -m pip install -r requirements.txt pyinstaller
"$PY" -m PyInstaller --noconfirm --windowed --name StreamSync \
    --collect-all imageio_ffmpeg \
    --collect-all soundcard \
    streamsync.py
echo "Built: dist/StreamSync.app"
