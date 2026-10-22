# StreamSync - first run on a Mac

Follow in order; each step proves the ground the next one stands on.
Total time: ~10 minutes. If any step fails, note the exact error and
where - that's gold for fixing it.

There are two ways in. Most people take **A**, the released app. **B**
runs StreamSync from source, for testing a change; it is steps 0 to 2.
Both meet again at step 3.

The app needs **macOS 14 (Sonoma) or later**. On anything older, macOS
says it needs a newer version of macOS and won't open it. **Apple menu >
About This Mac** shows your version.

## A. Install the released app

Download the `.dmg` for your Mac from the
[latest release](https://github.com/gBear97/StreamSync/releases/latest):
`arm64` for Apple Silicon, `x86_64` for Intel (**Apple menu > About This
Mac** says which). Open it and drag StreamSync.app into your
Applications folder, then open it from there. Releases are signed and
notarized, so it just opens.

Coming from 1.x? The old app offers 2.0 itself, or drag the new
StreamSync.app over the old one in Applications and choose **Replace**.
Your settings are kept.

Then go to step 3.

## B. From source

### 0. Python

Install Python 3.12 from **python.org** (the macOS installer). Avoid
the system Python and plain Homebrew Python - they often ship without
Tkinter, or with Tk 8.5, and the UI needs Tk 8.6. Check:

```
python3 -c "import tkinter; print(tkinter.TkVersion)"
```

### 1. Get the code, then install packages

```
git clone https://github.com/gBear97/StreamSync.git
cd StreamSync
python3 -m pip install -r requirements.txt
sh mac/build_sstap.sh
```

The last line builds `sstap`, the small helper that records this Mac's
sound (it needs Xcode's command-line tools: `xcode-select --install`).

### 2. Prove the engine (no UI, no VLC, no permissions involved)

```
python3 test_audio_matcher.py
```

It should end with `... TEST PASSED`. If it passes, the sync brain -
the hard part - is fully working on Apple hardware. Then launch:

```
python3 streamsync.py
```

## 3. The first-run check

The first-run check appears if anything is missing:

- **VLC**: one click (Homebrew) or the videolan.org button.

**Start StreamSync** unlocks once VLC is found (and, from source, the
Python packages are installed). The check also says whether StreamSync
can hear this Mac's sound - that never holds it back.

## 4. Let StreamSync hear this Mac (one time)

Nothing to install or route: StreamSync records what the Mac plays
through macOS's own audio tap, every app except itself. On the first
sync macOS asks whether StreamSync may record this computer's audio -
answer **Allow**. To check it later, or to change a "Don't Allow", look
under **System Settings > Privacy & Security > Screen & System Audio
Recording > System Audio Recording Only**. Running from source (B), the
prompt names your terminal instead of StreamSync.

**Settings > Listen to** shows *This Mac's sound* as the automatic
choice. BlackHole or a microphone can be picked there instead; a
microphone is for following a stream that plays on another device, and
hears it a moment late.

On macOS 14.0 or 14.1, which have no audio taps, install BlackHole
(`brew install blackhole-2ch`, or from
[existential.audio/blackhole](https://existential.audio/blackhole/)),
then in Audio MIDI Setup: **+** > **Create Multi-Output Device** > tick
your speakers **and** BlackHole 2ch > right-click it > **Use This Device
For Sound Output**. StreamSync picks BlackHole by itself, and macOS asks
for **Microphone** permission on the first sync.

## 5. First sync test (no stream needed)

Play any film in any player (QuickTime is fine), with its sound on. In
StreamSync open the *same* film file
(**Open film...**), optionally type a rough position under **Settings >
Start searching near**, and hit **Sync to stream**. The state line
under the buttons should read **Synced** (the match's score is in the
log), and playback starts at the right moment.

## 6. The film window

The film plays in StreamSync's own film window. Try:

- **Double-click** - fullscreen, and again to leave (or **Esc**).
- **Right-click** or **Control-click** - the menu: Play / Pause,
  Fullscreen, mute, volume, Subtitles, Hide video window.
- **Scroll** - volume, with the level shown top right.
- In fullscreen, **move the mouse** - the control bar: timeline, Pause,
  Stop, -1 / +1 frame, Resync here, Resync (full).
- Keys: **Space** play/pause, **← / →** 5 s back/forward, **F**
  fullscreen, **Esc** leave fullscreen.

## 7. Optional feature, with a one-time permission prompt

- **Settings > Show the stream while the film is paused**: on the first
  pause, macOS asks whether StreamSync may control *System Events*.
  Answer **Allow**. To check it later, or to change a "Don't Allow",
  look under **System Settings > Privacy & Security > Automation >
  StreamSync**. Running from source (B), the prompt names your terminal
  instead of StreamSync.

## 7.5 Watch parties (optional)

The **Watch party** button hosts or joins watch-party sessions. To try
it solo from a source checkout: run `python3 relay_server.py` in a
second terminal, open a film, then **Watch party > Start hosting** with
the relay server `ws://localhost:8765`. The Watch party window shows
your session code once the relay answers. Cross-machine tests need the
relay on a box both sides can reach - see
[Relay server](README.md#relay-server).

## 8. Build the app yourself (optional)

```
PYTHON=python3.12 sh build_app_mac.sh      # produces dist/StreamSync.app
```

## Known first-contact suspects

If something misbehaves, it's most likely one of these - all fixable:

- Widget spacing / fonts looking off in the main window.
- The film window staying black after a film is opened (expected: it
  opens with the film, and the picture starts with the first sync).
- `soundcard` errors naming a device -> send the exact device list from
  Settings > Listen to.
- Fullscreen on a second screen: not tested on a Mac yet. Note which
  screen the film window was on and what happened.

The log is at `~/Library/Logs/StreamSync/streamsync.log`, and
`/Applications/StreamSync.app/Contents/MacOS/StreamSync --diagnose`
(or `python3 streamsync.py --diagnose`) prints a report on VLC and the
app's own build - send both with any report.
