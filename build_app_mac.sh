#!/bin/sh
# Build StreamSync.app - run this ON a Mac, from this folder.
# VLC.app must still be installed on the machine that runs it.
set -e
python3 -m pip install -r requirements.txt pyinstaller
python3 -m PyInstaller --noconfirm --windowed --name StreamSync \
    --collect-all imageio_ffmpeg \
    --collect-all soundcard \
    streamsync.py
echo "Built: dist/StreamSync.app"
