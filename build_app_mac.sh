#!/bin/sh
# Build StreamSync.app - run this ON a Mac, from this folder.
# VLC.app must still be installed on the machine that runs it.
set -e
VERSION=$(python3 -c 'from version import __version__; print(__version__)')
python3 -m pip install -r requirements.txt pyinstaller
# certifi is collected explicitly: netcerts imports it lazily, so
# PyInstaller's import scan never sees it, and without its cacert.pem the
# bundle has no CA roots and every HTTPS call fails to verify.
python3 -m PyInstaller --noconfirm --windowed --name StreamSync \
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
