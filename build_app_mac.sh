#!/bin/sh
# Build StreamSync.app - run this ON a Mac, from this folder.
# VLC.app must still be installed on the machine that runs it.
#
# The app bundles the Python and Tk it is built with, and it does not run
# on Apple's /usr/bin/python3 (Python 3.9 with Tk 8.5) - which is what a
# bare python3 on a Mac often is. Set PYTHON to the one you run StreamSync
# with (default: python3):
#
#     PYTHON=python3.12 sh build_app_mac.sh
#
set -e
PY="${PYTHON:-python3}"

# Refuse the wrong Python here, rather than ship a bundle that dies on
# launch. Apple's Python is the one built as Python3.framework (a venv
# made from it keeps it as its base).
"$PY" - <<'PYCHECK'
import sys
if "/Python3.framework/" in sys.base_prefix:
    sys.exit("%s is Apple's Python %s - its Tk 8.5 cannot run StreamSync's "
             "UI, so the app would die on launch. Install Python from "
             "python.org (MAC_FIRST_RUN.md step 0) and build with it, e.g. "
             "PYTHON=python3.12 sh build_app_mac.sh"
             % (sys.executable, sys.version.split()[0]))
try:
    import tkinter
except ImportError:
    sys.exit("%s has no tkinter, which StreamSync's UI needs. Build with a "
             "python.org Python (MAC_FIRST_RUN.md step 0), e.g. "
             "PYTHON=python3.12 sh build_app_mac.sh" % sys.executable)
if tkinter.TkVersion < 8.6:
    sys.exit("%s has Tk %s; StreamSync's UI needs Tk 8.6 or newer. Build "
             "with a python.org Python (MAC_FIRST_RUN.md step 0), e.g. "
             "PYTHON=python3.12 sh build_app_mac.sh"
             % (sys.executable, tkinter.TkVersion))
PYCHECK

VERSION=$("$PY" -c 'from version import __version__; print(__version__)')
"$PY" -m pip install -r requirements.txt pyinstaller
# certifi is collected explicitly: netcerts imports it lazily, so
# PyInstaller's import scan never sees it, and without its cacert.pem the
# bundle has no CA roots and every HTTPS call fails to verify.
"$PY" -m PyInstaller --noconfirm --windowed --name StreamSync \
    --collect-all imageio_ffmpeg \
    --collect-all soundcard \
    --collect-all certifi \
    --hidden-import certifi \
    streamsync.py

PLIST=dist/StreamSync.app/Contents/Info.plist

# PyInstaller's CLI cannot set these, so stamp them in afterwards.
#
# plutil, not PlistBuddy: PlistBuddy re-parses its -c argument with a
# mini-parser that treats an apostrophe as an opening quote, so the usage
# strings below ("the stream's audio") died with "Parse Error: Unclosed
# Quotes" and were never written - silently, because the fallback Add
# failed the same way. plutil takes the value as a real argument.
plist_set() {
    plutil -replace "$1" -string "$2" "$PLIST" 2>/dev/null \
        || plutil -insert "$1" -string "$2" "$PLIST"
}

# What Finder's Get Info reads, and what tells two downloads apart.
plist_set CFBundleShortVersionString "$VERSION"
plist_set CFBundleVersion "$VERSION"

# macOS terminates an app that touches the microphone or drives another
# app without a usage string here - the prompt has nothing to display, so
# TCC kills the process rather than asking. These are the strings the
# system shows in its permission dialogs.
plist_set NSMicrophoneUsageDescription \
    "StreamSync listens to a few seconds of the stream's audio to find that exact moment in your local copy of the film."
plist_set NSAppleEventsUsageDescription \
    "StreamSync brings the stream's browser window forward while the film is paused, and hides it again when playback resumes."

# Read them back. The failure above was silent, and an Info.plist missing
# a usage string does not misbehave until macOS terminates the app the
# first time it touches the microphone - by which point it is a user's
# crash, not a build error.
for key in CFBundleShortVersionString CFBundleVersion \
           NSMicrophoneUsageDescription NSAppleEventsUsageDescription; do
    value=$(plutil -extract "$key" raw -o - "$PLIST" 2>/dev/null || true)
    if [ -z "$value" ]; then
        echo "Info.plist is missing $key - the app would be terminated by"
        echo "macOS the first time it needed the matching permission."
        exit 1
    fi
    echo "  $key = $value"
done

echo "Built: dist/StreamSync.app (version $VERSION)"
