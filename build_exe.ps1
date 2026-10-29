# Build a standalone StreamSync.exe (no Python needed to run it).
# Output: dist\StreamSync\StreamSync.exe
# VLC must still be installed on the machine that runs the exe.

# constraints.txt: the exact versions CI and the releases are built with.
pip install --disable-pip-version-check -c constraints.txt pyinstaller
if (-not $?) { exit 1 }

# invoked as a module: pip's user-install Scripts dir may not be on PATH
python -m PyInstaller --noconfirm --windowed --name StreamSync `
    --collect-all imageio_ffmpeg `
    --collect-all soundcard `
    --hidden-import websockets.asyncio.server `
    streamsync.py
if (-not $?) { exit 1 }

Write-Output "Built: $(Resolve-Path dist\StreamSync\StreamSync.exe)"
