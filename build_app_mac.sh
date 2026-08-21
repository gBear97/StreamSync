#!/bin/sh
# Build StreamSync.app - run this ON a Mac, from this folder.
# VLC.app must still be installed on the machine that runs it.
set -e
VERSION=$(python3 -c 'from version import __version__; print(__version__)')
python3 -m pip install -r requirements.txt pyinstaller
python3 -m PyInstaller --noconfirm --windowed --name StreamSync \
    --collect-all imageio_ffmpeg \
    --collect-all soundcard \
    streamsync.py

# PyInstaller's CLI has no flag for the bundle version, so stamp it in
# afterwards - this is what Finder's Get Info panel reads, and what tells
# two downloaded copies apart once they are out of their .dmg.
PLIST=dist/StreamSync.app/Contents/Info.plist
for KEY in CFBundleShortVersionString CFBundleVersion; do
    /usr/libexec/PlistBuddy -c "Set :$KEY $VERSION" "$PLIST" 2>/dev/null \
        || /usr/libexec/PlistBuddy -c "Add :$KEY string $VERSION" "$PLIST"
done

echo "Built: dist/StreamSync.app (version $VERSION)"
