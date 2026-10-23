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

# The oldest macOS the app runs on. It is the newest that anything bundled
# was built for: numpy's wheels need 14.0 (_multiarray_umath carries minos
# 14.0), so an older Mac crashed on launch with nothing said. Stamped into
# Info.plist below, so macOS refuses it plainly instead, and checked
# against every binary in the bundle. README.md and MAC_FIRST_RUN.md say
# "macOS 14 (Sonoma) or later": change them with it.
MIN_MACOS=14.0

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
    --hidden-import websockets.asyncio.server \
    streamsync.py

PLIST=dist/StreamSync.app/Contents/Info.plist

# "This Mac's sound" is recorded by sstap, a small Swift helper
# (mac/sstap.swift), built for the same floor as everything else here and
# put beside StreamSync's own executable, where audio_capture finds it.
MIN_MACOS="$MIN_MACOS" sh mac/build_sstap.sh dist/StreamSync.app/Contents/MacOS/sstap

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

# On anything older, macOS says the app needs a newer macOS rather than
# letting it start and crash.
plist_set LSMinimumSystemVersion "$MIN_MACOS"

# macOS terminates an app that touches the microphone or drives another
# app without a usage string here - the prompt has nothing to display, so
# TCC kills the process rather than asking. These are the strings the
# system shows in its permission dialogs.
plist_set NSMicrophoneUsageDescription \
    "StreamSync listens to a few seconds of the stream's audio to find that exact moment in your local copy of the film."
plist_set NSAppleEventsUsageDescription \
    "StreamSync brings the stream's browser window forward while the film is paused, and hides it again when playback resumes."
# The tap that records "This Mac's sound" (sstap). sstap is StreamSync's
# child, so macOS asks in StreamSync's name and shows this string.
plist_set NSAudioCaptureUsageDescription \
    "StreamSync listens to a few seconds of what this Mac is playing - the stream's sound - to find that exact moment in your copy of the film."

# Read them back. The failure above was silent, and an Info.plist missing
# a usage string does not misbehave until macOS terminates the app the
# first time it touches the microphone - by which point it is a user's
# crash, not a build error.
for key in CFBundleShortVersionString CFBundleVersion LSMinimumSystemVersion \
           NSMicrophoneUsageDescription NSAppleEventsUsageDescription \
           NSAudioCaptureUsageDescription; do
    value=$(plutil -extract "$key" raw -o - "$PLIST" 2>/dev/null || true)
    if [ -z "$value" ]; then
        echo "Info.plist is missing $key - the app would be terminated by"
        echo "macOS the first time it needed the matching permission."
        exit 1
    fi
    echo "  $key = $value"
done

# LSMinimumSystemVersion is only true if nothing inside needs more. A
# dependency update can raise the floor without a word - numpy did - so
# read what every Mach-O file in the bundle was built for (otool, per
# architecture) and stop the build if any of it needs a newer macOS.
"$PY" - dist/StreamSync.app "$MIN_MACOS" <<'PYFLOOR'
import os, subprocess, sys

app, floor = sys.argv[1], sys.argv[2]
MACHO = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",   # 64- and 32-bit
         b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}   # universal


def ver(text):
    return tuple(int(p) for p in text.split("."))


def needs(path):
    """The newest macOS any architecture in this file was built for."""
    out = subprocess.run(["otool", "-arch", "all", "-l", path],
                         capture_output=True, text=True).stdout
    found, cmd = [], None
    for line in out.splitlines():
        words = line.split()
        if words[:1] == ["cmd"]:
            cmd = words[1]
        elif cmd == "LC_BUILD_VERSION" and words[:1] == ["minos"]:
            found.append(words[1])
        elif cmd == "LC_VERSION_MIN_MACOSX" and words[:1] == ["version"]:
            found.append(words[1])
    return max(found, key=ver, default=None)


newest, too_new, files = None, [], 0
for root, _, names in os.walk(app):
    for name in names:
        path = os.path.join(root, name)
        if os.path.islink(path):
            continue
        with open(path, "rb") as f:
            if f.read(4) not in MACHO:
                continue
        files += 1
        v = needs(path)
        if v is None:
            continue
        rel = os.path.relpath(path, app)
        if newest is None or ver(v) > ver(newest[0]):
            newest = (v, rel)
        if ver(v) > ver(floor):
            too_new.append(f"  {v}  {rel}")

print(f"  {files} Mach-O files; the newest macOS any needs is "
      f"{newest[0]} ({newest[1]})" if newest else f"  {files} Mach-O files")
if too_new:
    sys.exit(f"These need a macOS newer than {floor}, the floor "
             "LSMinimumSystemVersion promises:\n" + "\n".join(too_new) +
             "\nRaise MIN_MACOS in build_app_mac.sh, and the macOS version "
             "README.md and MAC_FIRST_RUN.md state with it - or pin the "
             "dependency that raised it.")
PYFLOOR

# Adding sstap after PyInstaller broke the bundle's ad-hoc seal, if it
# made one: re-seal it, so a local build still verifies and launches. A
# release signs everything again with the real identity (release.yml).
if [ -d dist/StreamSync.app/Contents/_CodeSignature ]; then
    codesign --force --sign - dist/StreamSync.app
fi

echo "Built: dist/StreamSync.app (version $VERSION)"
