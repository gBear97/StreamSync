# StreamSync - first run on a Mac

Follow in order; each step proves the ground the next one stands on.
Total time: ~10 minutes. If any step fails, note the exact error and
where - that's gold for fixing it.

## 0. Python

Install Python 3.11+ from **python.org** (the macOS installer). Avoid
the system Python and plain Homebrew Python - they often ship without
Tkinter, which the UI needs. Check:

```
python3 -c "import tkinter; print(tkinter.TkVersion)"
```

## 1. Copy the whole StreamSync folder over, then install packages

```
cd StreamSync
python3 -m pip install -r requirements.txt
```

## 2. Prove the engine (no UI, no VLC, no permissions involved)

```
python3 test_audio_matcher.py
```

It should end with `... TEST PASSED`. If it passes, the sync brain -
the hard part - is fully working on Apple hardware.

## 3. Launch

```
python3 streamsync.py
```

The first-run gate appears if anything is missing:

- **VLC**: one click (Homebrew) or the videolan.org button.
- **BlackHole**: one click - downloads the signed ~100 KB installer,
  verifies its checksum, then macOS asks for your password.

## 4. Audio routing (one time)

Click **Open Audio MIDI Setup**, then: **+** (bottom-left) > **Create
Multi-Output Device** > tick your speakers/headphones **and**
BlackHole 2ch. Right-click the new device > **Use This Device For Sound
Output**. In StreamSync: **Settings > Listen to > BlackHole 2ch**.

macOS will ask for **Microphone** permission on the first sync - allow it.

## 5. First sync test (no stream needed)

Play any film in any player (QuickTime is fine) with audio going through
the Multi-Output Device. In StreamSync open the *same* film file, type a
rough position hint (Settings > Start searching near), hit **Sync to
stream**. It should match with a high score and start VLC playback at
the right moment.

## 6. Optional feature, with a one-time permission prompt

- **Settings > Show the stream while the film is paused**: needs
  *Automation* permission (System Events) on first use.

## 6.5 Watch parties (optional)

The **Watch party** button hosts or joins watch-party sessions. To try
it solo: run `python3 relay_server.py` in a second terminal, then
**Watch party > Start hosting** with the relay server
`ws://localhost:8765`. The Watch party window shows your session code
once the relay answers. Cross-machine tests need the relay on a box
both sides can reach.

## 7. When everything works

```
sh build_app_mac.sh        # produces dist/StreamSync.app
```

## Known first-contact suspects

If something misbehaves, it's most likely one of these - all fixable:

- Widget spacing / fonts looking off in the main window.
- The film window staying black after a film is opened (expected: it
  opens with the film, and the picture starts with the first sync).
- `soundcard` errors naming a device -> send the exact device list from
  Settings > Listen to.
