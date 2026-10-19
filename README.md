# StreamSync

Watch a live stream for the streamer's commentary audio, while your own
high-res copy of the film plays the picture - kept in sync.

StreamSync listens to a few seconds of your system audio (the stream's
sound, commentary and all), finds that exact moment in your local file,
and seeks VLC to it - compensating for the seconds the search itself
takes. The stream can stay **minimized**; only its audio needs to play.
It runs on Windows and macOS, with the same window and the same controls
on both.

## What's new in 2.0

- **One app for both systems, in a new dark design.** Windows and the Mac
  share the same main window, Settings, Watch party and Update screens.
  A first-run screen walks you through opening the film; once a film is
  open, the main window shows the sync state, the player and fine-tune.
- **Windows ships as an installer.** The download is a per-user setup
  `.exe`: no admin prompt, and the app can then update itself.
- **Signed updates.** Every release's checksum list is signed, and both
  apps refuse an update unless that list's signature checks out and the
  download matches its checksum on the list. An update is offered in its
  own window, with progress and a Cancel button.
- **The film window works the same on the Mac as on Windows:**
  double-click for fullscreen, right-click (or Control-click) for a menu,
  scroll for volume, and a control bar in fullscreen.

### Removed in 2.0

- **Video sync** (screen capture, capture region, facecam ignore zones,
  mirror toggle). Syncing is audio-only. The Mac no longer asks for
  Screen Recording permission.
- **The ±2 s nudge buttons** and **F11**. Fine-tune now steps by 0.1 s and
  0.5 s; **F** toggles fullscreen.
- **The Search ± box** beside the position hint. A sync with a hint always
  searches 2 minutes either side of it, 1.x's default; a different width
  set in 1.x is not kept.
- **Global hotkeys** on Windows (Ctrl+Alt+S and friends) and the Mac's
  menu shortcuts (Command-S, Command-R, Command-P). The app's keys work
  while one of its windows has the keyboard - see [Keys](#keys).
- **The Windows zip.** Releases carry the setup `.exe` instead. A copy
  unzipped from a 1.x release cannot update itself - see
  [Updating](#updating).

## Requirements

- Windows (64-bit), or macOS on Apple Silicon or Intel
- VLC installed (64-bit on Windows). The first-run check offers to
  install it.
- On the Mac: the free BlackHole audio driver - see
  [macOS setup](#macos-setup)

## Install

**Windows**: download `StreamSync-<version>-windows-x64-<commit>-setup.exe`
from the [latest release](https://github.com/gBear97/StreamSync/releases/latest)
and run it. It installs for your user only, into
`%LOCALAPPDATA%\Programs\StreamSync`, without an admin prompt, and adds
StreamSync to the Start menu (a desktop icon is optional). The folder
cannot be changed: it is the one place the app's updater replaces.

The installer is not code-signed, so the first install from the browser
may show SmartScreen's "Windows protected your PC": **More info > Run
anyway**. To uninstall, use Windows' **Settings > Apps** like any other
program.

**macOS**: download the `.dmg` for your Mac - `arm64` for Apple Silicon,
`x86_64` for Intel (**Apple menu > About This Mac** says which) - open it,
and drag StreamSync.app into your Applications folder. Releases are
signed and notarized, so the app just opens. On the first launch the
first-run check offers to install VLC if it is missing (and BlackHole
with it), you install BlackHole if you don't have it yet, and you set up
the audio routing once in Audio MIDI Setup;
[MAC_FIRST_RUN.md](MAC_FIRST_RUN.md) walks through it step by step.

### First-run check

On startup StreamSync verifies its dependencies: VLC (64-bit), the
bundled ffmpeg, BlackHole on the Mac, and - when running from source -
the Python packages. If everything is present the check is invisible. If
something is missing, a fix-it dialog appears with one-click installs:

- **VLC** on Windows - installs through **winget**, Windows' built-in
  package manager: the download is hash-verified by Microsoft's
  repository and the installer is VideoLAN's own signed one. Windows
  shows a normal admin (UAC) prompt during install. If winget isn't
  available, a button opens the official videolan.org download page
  instead, and the dialog re-checks automatically once you've installed
  it.
- **VLC** on the Mac - through Homebrew if you have it, otherwise the
  videolan.org button.
- **BlackHole** on the Mac - one click; see [macOS setup](#macos-setup).
- **Python packages** (source only) - runs pip for you and re-checks.

Each fix shows its progress in the dialog and confirms when done; the
**Start StreamSync** button unlocks once VLC, ffmpeg and the packages
pass. BlackHole does not hold it back, and a missing BlackHole alone
does not open the check: without it StreamSync starts, but it hears the
Mac's microphone instead of the stream. See
[macOS setup](#macos-setup) for installing it.

## Updating

StreamSync checks GitHub for a newer release when it starts (turn that
off under **Settings > Updates**), and **Settings > Updates > Check now**
asks on demand. When something newer exists, an Update window offers it,
with a link to what's new. **Install and restart** downloads it with a
progress bar and a **Cancel** button, then:

1. checks that the release's `SHA256SUMS` carries a valid signature by
   StreamSync's release key (`SHA256SUMS.sig`; the public key is built
   into the app);
2. checks the download against its line in those signed checksums;
3. on the Mac, also checks that the new app's code signature is intact
   and issued to StreamSync's own Developer ID team.

If any check fails, nothing is installed and your copy is left exactly as
it was; the window says why and offers the download page. Once
everything checks out:

- **Windows** runs the downloaded installer silently. It closes
  StreamSync, replaces the installed copy and starts the new version.
- **macOS** copies the new app beside the old one, quits, swaps them,
  and relaunches. If the swap fails, the old app is put back.

Settings (`~/.streamsync.json` in your home folder) and the log are
outside the app's folder, so an update, a reinstall or an uninstall
leaves them alone.

### Coming from 1.x

- **Windows, from a 1.x zip**: 1.x on Windows never updated itself, and
  2.0 only updates a copy the installer put in place. So install 2.0
  once by hand: download and run the setup `.exe`, then delete the old
  unzipped StreamSync folder. Your settings are kept - they live in your
  home folder, not in that folder. From then on, updates come in-app.
  (A 2.0 copy run from anywhere other than the installer's folder offers
  the Releases page instead of installing.)
- **macOS**: 1.x offers 2.0 in-app as usual. Or download the `.dmg` and
  drag the new StreamSync.app over the old one in Applications
  (**Replace**).
- **macOS 1.0.0 and 1.0.1** cannot update themselves: those builds shipped
  without CA certificates, so their update check never reached GitHub.
  Download a newer release once by hand.

A copy running from a source checkout never replaces itself: on the Mac
it tells you to `git pull`, and on Windows it offers the Releases page.
Update a checkout with `git pull`.

## Antivirus notes

- On Windows, StreamSync downloads its own installer and runs it to
  update itself. It only runs an installer whose checksum matches the
  release's signed checksum list, but "a program fetched an exe and ran
  it" is exactly what some antivirus tools are suspicious of, so yours
  may flag or block an update. If it does, download the setup `.exe`
  from the [releases page](https://github.com/gBear97/StreamSync/releases/latest)
  and run it yourself - it updates the same installation.
- The app and its installer are unsigned PyInstaller and Inno Setup
  builds. SmartScreen may show "unknown publisher" for a first install
  from the browser: **More info > Run anyway**. An installer the app
  downloaded itself carries no mark of the web, so SmartScreen does not
  stop an in-app update.
- VLC installs go through winget (verified) or your own browser.
- Loopback audio recording uses the same standard Windows API (WASAPI)
  as OBS; it doesn't normally trigger anything.

## Quick start

1. Play the stream in your browser with its sound on. Check under
   **Settings > Listen to** that StreamSync listens to the device the
   stream plays through. (Before a film is open, the first-run screen's
   "Listening to" line says the same, and its **change** link opens that
   setting.)
2. **Open film...** - pick your local copy of the film.
3. Optionally type a rough position under **Settings > Start searching
   near** (e.g. `1:23:00`): a sync then searches 2 minutes either side of
   it instead of the whole film.
4. Hit **Sync to stream**. It takes the last ~6 s of stream audio, finds
   the moment, and starts playback there. The film's own sound is muted by
   default - the stream provides the sound. (StreamSync keeps listening
   once it has started, so after the first sync the audio is usually
   already there and a sync starts searching immediately.)
   If the match is weak it listens longer - 12 s, then 18 s - before
   giving up. A weak result is only ever used for the very first sync;
   once the film has a position, a weak Resync leaves it where it is
   and says so, rather than throwing it somewhere random.

The sync-state line under the buttons says what happened - **Synced**,
**Weak match**, **No match**, or **Moved by hand** after you seek - and
how long ago.

**Resync** searches around where the film is now - mostly backwards,
since a pause on the stream leaves your copy ahead.

### Player

Under **Settings > Player**:

- *Built-in player* (default): the film plays in StreamSync's own film
  window. Every sync is checked once playback has restarted and any
  remainder is trimmed away - lands within ~10 ms. Fullscreen bar and
  subtitle picker included.
- *VLC app*: your normal VLC opens and StreamSync drives it remotely. Its
  remote interface only seeks on whole seconds, so StreamSync times each
  seek to fire at exactly the right instant, then checks and trims it the
  same way. Use VLC's own menus for subtitles and audio tracks in this
  mode.

### Fine-tune

The **Fine-tune** row nudges the film against the stream: **−0.5**,
**−0.1**, **+0.1**, **+0.5** seconds, with the total shown in between and
**Reset** to clear it. While playing, a nudge runs the film 5% fast or
slow until it has moved by exactly that much - smooth, no seek, no
freeze; while paused, it steps exactly. Nudges add up to an offset that
every later sync keeps, so a nudge is not "corrected" away.

## Keep in sync automatically

Turn on **Keep in sync automatically** and StreamSync quietly re-checks
sync in the background, every 30 s by default (**Settings > Re-check
every**, 10 s at the least):

- Each check records 4 s of audio (8 s if the first look is weak) and
  scans about 45 s either side of where the film *should* be - roughly
  one second of CPU work per check, then it sleeps. Raise the interval if
  you want it even lighter.
- Drift beyond 0.35 s is corrected automatically. Your fine-tune offset
  is part of where the film *should* be, so it is kept.
- With **Settings > Follow the stream's pauses** on: when the film's
  audio disappears from the stream for two consecutive checks (streamer
  paused, or is talking over a black screen), your copy pauses too. When
  the film's audio comes back, it resyncs and resumes automatically.
  Fully hands-free. If the stream was actually still playing (a stretch
  too quiet to hear), the resume search widens with every check until it
  finds it again.
- If checks keep failing the same way, auto mode turns itself off and
  says why.

## See the streamer during pauses

With **Settings > Show the stream while the film is paused** on, a
detected pause (auto mode, or you pausing yourself) brings the stream's
browser forward so you see the facecam while the streamer talks, and
tucks your film window out of the way. When the film starts again, the
browser goes back and the film comes back (fullscreen restored if it was
fullscreen).

- **Windows**: pick the browser window under **Stream window** (Refresh
  lists open windows; *Automatic* picks a Twitch or Kick window). The
  window is restored, then minimized again. "Piping" the stream's video
  into the app isn't possible while the window is minimized - Windows
  doesn't render minimized windows - so restoring the real window is both
  the cheapest and the only way to show it.
- **macOS**: pick the browser app under **Stream app** (*Automatic* takes
  the first browser it finds). The app comes forward during pauses and
  hides on resume. macOS asks for *Automation* permission (System
  Events) the first time.

## The film window

The built-in player draws into StreamSync's film window. On both systems:

- **Double-click** toggles fullscreen. On Windows fullscreen is
  borderless, on the monitor the film window is on; on the Mac it is the
  window's own fullscreen.
- **Right-click** (or **Control-click** on a Mac) opens a menu: Play /
  Pause, Fullscreen, Mute film sound, Volume up and down, Subtitles (the
  film's tracks, or Load subtitle file...), Hide video window.
- **Scroll** changes the volume in steps of 5%, up to 125%, with an
  on-screen readout. Scrolling never unmutes the film: muted is what keeps
  the film's sound out of what StreamSync listens to.
- In **fullscreen**, moving the mouse shows a control bar along the bottom;
  it hides again with the pointer after a couple of seconds at rest. It
  has the timeline (click or drag to seek), **Pause**, **Stop**, **−1
  frame** and **+1 frame** (fine-tune by one frame, kept in the offset
  like any nudge), **Resync here** (around the current position) and
  **Resync (full)** (the whole film, even with a position hint set), plus
  the latest status.

Closed the film window? **Show film window** under the film's name in the
main window brings it back.

Known gap: on the Mac, fullscreen on a second screen has not been tested
yet.

## Keys

These work in the main window, the film window and the fullscreen bar
(not while you are typing in a field). They are also listed under
**Settings > Keys**.

| Key | Action |
| --- | --- |
| Space | Play / pause |
| ← / → | Back / forward 5 s |
| F | Fullscreen |
| Esc | Leave fullscreen |

On the Mac, **Command-,** opens Settings and **Command-Q** quits.

## Watch party

For streamers who don't want any film audio or video on their stream at
all: the host streams **only themselves**, and every viewer plays their
own copy of the film, kept in sync by StreamSync. Nothing copyrighted
ever crosses the network - the entire protocol is positions, UTC
timestamps and non-invertible fingerprint hashes (sign-bit signatures
from which audio cannot be reconstructed; a whole film's fingerprint is
~56 KB of hashes).

Open the film first, then **Watch party** at the bottom of the main
window. Both sides set the **Relay server** at the bottom of that window
(see [Relay server](#relay-server) for running one).

**Host**: choose where the film's position comes from - *What this
computer hears* (play the film in any player; StreamSync follows it by
listening) or *This app's player*. Pick your microphone: it is
fingerprinted continuously so viewers can measure their personal stream
delay against your voice. A password is optional; the **Delay hint for
viewers** (10 s by default, 2 to 45 s) stands until each viewer has
measured their own. **Start hosting**, then share the session code the window shows
with your viewers.

**Join**: enter the code (and password), then **Join**. Your file is
verified against the host's hashes first - different bitrates and
releases are fine (a constant offset is detected and absorbed); a
different cut or the wrong film is refused with a clear message rather
than synced wrong. Then playback follows the host's timeline, delayed by
*your* measured stream delay, so the film on your screen matches the
commentary in your ears - and when the host pauses, your copy pauses at
the moment the commentary about it reaches you.

While a session runs, the Watch party window shows its code and latest
status, and **Leave session**; a dot beside the main window's Watch party
button says one is running.

Under the hood: both ends sync to internet time (NTP) so "position X at
time T" means the same instant everywhere - machine clocks are often
off by hundreds of milliseconds (this machine: 404 ms). The correction
is re-measured every 10 minutes and is immune to the OS stepping its own
clock mid-film. The host broadcasts a delay hint that applies
immediately; each viewer's client then refines it automatically by
finding the host's voice fingerprints in their own incoming stream
audio - to within about 10 ms, well below the fingerprints' 250 ms
spacing, by re-aligning its own audio at sub-step offsets. Fine-tune
works for viewers too: it shifts your copy against the host's timeline.

A host playing the film in another player ("What this computer hears")
is followed by its sound: a pause is placed where the audio actually
stopped, and it takes two silent checks in a row, so a quiet scene does
not pause everyone. For frame-exact pauses and seeks, host from
StreamSync's own player.

If a connection drops, both ends reconnect on their own. The relay holds
a host's room for a minute (viewers see "host away") and gives it back
only to that host.

### Relay server

A watch party runs through a relay: a small program the host and every
viewer connect to, which passes the session's messages along. The apps
do not include one, so someone runs it where both sides can reach it.
There are two ways.

**No server needed.** The host runs the relay on their own computer and
a free [Cloudflare quick tunnel](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)
makes it reachable from anywhere. No account, nothing to rent, nothing
to open in a router or firewall.

1. Set up and start the relay as in steps 1 to 3 below, on the host's
   computer.
2. Install `cloudflared` - on a Mac, `brew install cloudflared`; on
   Windows, the MSI from Cloudflare's
   [downloads page](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/) -
   and, in a second window, run

   ```
   cloudflared tunnel --url http://localhost:8765
   ```

   After a few seconds it prints an address like
   `https://some-random-words.trycloudflare.com`.
3. Everyone, host included, pastes that address into **Relay server**.
   StreamSync connects to the `wss://` address it stands for -
   `wss://some-random-words.trycloudflare.com` - and the field shows it
   so once Host or Join is pressed.

Keep both windows open while the party lasts. The address changes every
time `cloudflared` starts, so share the new one each time. This suits a
group of friends rather than a large audience: the relay is only up
while the host's computer is, and a quick tunnel is a free service
Cloudflare offers for testing, with a cap on how much it carries at
once.

If the party cannot reach the relay, the message says why when it can
tell: a certificate that did not verify, say, or a tunnel answering
HTTP 502, which means the relay behind it is not running.

**On a server.** For a relay that is always there, run it on a machine
both sides can reach - a $5 VPS or small AWS instance serves thousands
of viewers (traffic is a few tiny messages per second per room, and a
slow viewer never holds up the others). On that machine:

1. Install Python 3 (on Debian or Ubuntu, `python3-venv` as well).
2. Download `relay_server.py` from this repository and install the one
   package it needs, `websockets`. On Linux or macOS:

   ```
   curl -O https://raw.githubusercontent.com/gBear97/StreamSync/main/relay_server.py
   python3 -m venv relay-env
   relay-env/bin/pip install websockets
   ```

   (On Windows the last line is `relay-env\Scripts\pip install websockets`.)
3. Start it, and keep it running while the party lasts:

   ```
   relay-env/bin/python relay_server.py --port 8765
   ```

   (On Windows, `relay-env\Scripts\python relay_server.py --port 8765`.)
   It prints `StreamSync relay listening on :8765`. `--host-grace N`
   sets how long a dropped host's room waits for it (default 60 s).
4. Let inbound TCP port 8765 through that machine's firewall - on a VPS
   or AWS, in its firewall rules or security group.

Then the host and every viewer set **Relay server** in the Watch party
window to `ws://<that machine's address>:8765` - the same address on
all sides. The default, `ws://localhost:8765`, only reaches a relay
running on the same computer.

## macOS setup

1. **VLC** - the first-run check installs it via Homebrew or points you at
   videolan.org.
2. **BlackHole 2ch** (needed for audio sync) - macOS has no built-in way
   to record "what's playing", so audio is captured through this free,
   signed virtual audio driver. Install it with
   `brew install blackhole-2ch`, or get the BlackHole 2ch installer from
   [existential.audio/blackhole](https://existential.audio/blackhole/).
   When the first-run check opens for something else (VLC, say), it also
   offers BlackHole in **one click**: it downloads the developer's
   ~100 KB signed installer from their official URL, verifies the sha256
   against Homebrew's independently published checksum, and hands it to
   Apple's own installer (which also verifies the signature/notarization)
   - you just enter your password. A missing BlackHole on its own does not
   open the check.

   Then the one manual step: hit **Open Audio MIDI Setup** in the check
   (or open Audio MIDI Setup from **Applications > Utilities**), click
   **+ > Create Multi-Output Device**, tick your speakers *and*
   BlackHole 2ch, and set it as the sound output. StreamSync listens to
   BlackHole automatically; **Settings > Listen to** shows it. If the
   automatic choice there names a microphone instead, BlackHole is not
   installed.
3. **Permissions** - macOS will prompt as features are first used:
   *Microphone* (recording from BlackHole) and *Automation/System Events*
   (only for showing the stream while the film is paused).

Step by step, with a first sync test: [MAC_FIRST_RUN.md](MAC_FIRST_RUN.md).

## How it works

- **Audio**: 26 log-spaced band energies every 16 ms, level-normalized,
  matched by normalized cross-correlation against the file's audio track
  (decoded by a bundled ffmpeg - audio-only decode is fast, so even
  whole-file scans take well under a minute). Each candidate moment is
  normalized against its own loudness, so a quiet dialogue scene scores
  as well as a loud one. Under loud commentary the film survives mostly
  in the gaps between the streamer's words, where ducking also lets it
  back up - so the recording's quiet moments count for more, and it is
  also compared in 128 ms pieces, keeping at each moment the half that
  line up best. Every match reports how far it stands above everything
  else in the search (peak z) and above the best other candidate
  (score), both in standard deviations; weak matches are flagged rather
  than trusted. Files whose timestamps do not start at zero (.m2ts, .ts,
  some MP4s) are handled.
- **Timing**: stream audio is recorded continuously and every sample is
  dated by its position in the recording, not by when a recording was
  asked for - so the matched moment is corrected by exactly how long ago
  it was heard (the stream kept playing meanwhile), plus your fine-tune
  offset. VLC's own position only updates every 250-500 ms, so
  StreamSync dates each update as it arrives (within ~1 ms of the frame
  on screen). A seek stalls playback for an amount that depends on the
  file and the CPU (75 ms to over half a second), so after every seek
  StreamSync measures where playback really landed, trims the rest with a
  5% speed change, and learns the stall for next time: syncs land within
  about 10 ms, measured against the frames actually displayed. Every
  match decision (window, score, z, drift, applied or not) is written to
  the log, so a session that went wrong leaves the evidence behind.

## Tips & troubleshooting

### When something fails, ask the app why

StreamSync keeps a log:

- Windows: `%USERPROFILE%\.streamsync\streamsync.log`
- macOS: `~/Library/Logs/StreamSync/streamsync.log`

A crash at startup is also written to `.streamsync-error.log` in your
home folder. On the Mac (or from source) the app can describe its own
environment:

```
/Applications/StreamSync.app/Contents/MacOS/StreamSync --diagnose
```

That prints which libvlc it found, the architecture it was built for,
who signed it, whether this process can actually load it, and what macOS
said if it could not. The report is also written to the log
automatically whenever the player fails to start, so the evidence exists
without anyone having to reproduce the failure on purpose.

This matters most for one case that used to be invisible: a VLC built
for a different processor than StreamSync. An Intel VLC will not load
into an Apple Silicon build (or the reverse), and the old error said
"Install VLC from videolan.org" - advice that cannot work, because VLC
was already installed. The report names the mismatch instead.

### Getting good matches

- Audio sync assumes the stream is the *loudest thing* on that playback
  device. Pause your music first, or route the stream to its own device
  and pick it under **Settings > Listen to**.
- A streamer who talks without a pause over a quiet scene is the hard
  case: the film is only heard in the gaps. If a match comes back weak,
  try again when they pause, or set a position under **Settings > Start
  searching near** so the search covers less of the film.
- A streamer who ducks the film's audio very low gives weak matches.
  There is no video method to fall back on any more: sync again at a
  moment where the film is louder (music, action, a pause in the talk).
- 4K HEVC HDR / Dolby Vision copies: audio sync never touches the video
  track, so big files sync just as fast. For HDR playback quality itself,
  VLC tonemaps; choose the *VLC app* player if you have a tuned VLC
  setup for HDR.
- Streams run 5-30 s behind live; that's irrelevant to sync (you're
  syncing to what you *hear*), but it means your copy will be "behind
  live" too - as it should be.
- Settings (film, position hint, listening device, auto options, stream
  window, relay server, update check) persist in `~/.streamsync.json`.

## Run from source

Python 3.12 (64-bit, with Tk 8.6 - CI tests on 3.12) and VLC:

```
pip install -r requirements.txt
python streamsync.py          # the first-run check, then the app
python -m ui.shell            # the app without the first-run check
```

`python streamsync.py --selftest` opens the app, tours every dialog and
closes it again, with a throwaway settings file; it exits 0 when it
worked. On a Mac use a python.org Python: Apple's own `/usr/bin/python3`
has Tk 8.5, which cannot run the UI.

### Build it yourself

Windows - the app, then the installer
([Inno Setup 6](https://jrsoftware.org/isinfo.php)):

```
powershell -File build_exe.ps1                       # dist\StreamSync\StreamSync.exe
iscc /DAppVersion=2.0.0 installer\StreamSync.iss     # dist\StreamSync-setup.exe
```

macOS (run on the Mac):

```
PYTHON=python3.12 sh build_app_mac.sh                # dist/StreamSync.app
```

No Mac at hand to build on? The **Build macOS app** GitHub Actions
workflow compiles `StreamSync.app` on GitHub's macOS machines - for both
Apple Silicon (`arm64`) and Intel (`x86_64`) - on every push to `main`,
on pull requests, or on demand (Actions tab > Build macOS app > Run
workflow). Each run attaches a disk image named for the build - e.g.
`StreamSync-2.0.0-arm64-129a805.dmg`, the version from `version.py` plus
the commit it came from. These per-commit builds are **not signed** -
only tagged releases are - so clear the download quarantine once
(`xattr -dr com.apple.quarantine /Applications/StreamSync.app`) and use
right-click > **Open** the first time. For an ordinary install take a
[release](https://github.com/gBear97/StreamSync/releases) instead.

### Tests

```
python -m ui.contract         # which ui/ module owns each shared method (no Tk needed)
python test_audio_matcher.py  # audio: commentary noise, quiet scenes, offset files
python test_controller.py     # sync decisions against a simulated stream
python test_session.py        # a whole watch party on localhost
python test_updater.py        # versions, assets, signed checksums
python test_updateflow.py     # the update flow over a fake updater
python test_ui.py             # every screen under real Tk
```

The Tests workflow runs the full set on Windows, macOS and Linux for
each pull request: `test_audio_capture`, `test_players`,
`test_controller`, `test_audio_matcher`, `test_fingerprint`,
`test_relay`, `test_relay_tls`, `test_session`, `test_updater`,
`test_netcerts`, `test_diagnostics`, `test_macswap`, `test_windowctl`,
`test_videomouse`, `test_widgets`, `test_updateflow` and `test_ui`. Tests that drive real
windows skip themselves where they cannot run. Set `STREAMSYNC_SHOTS` to
a folder and `test_ui` saves each screen there as a PNG. Those tests
guard against regressions; for how well the audio matcher does on real
film audio under real commentary, see `bench_audio.py`.

Two more run by hand on a Mac, because they take over the screen:
`test_macwindowctl.py` (bringing an app forward and hiding it) and
`test_macapp_live.py` (the real app through pause, resume, fullscreen
and the VLC app; about three minutes).

`python -m ui.mainwin` and `python -m ui.dialogs` show galleries of the
main window and the dialogs over a stand-in app.

## Releasing (maintainers)

To cut a release, bump `__version__` in `version.py` and write the
release notes in `release-notes/v<version>.md` (what's new, what's gone,
how to upgrade - written for users), merge both to `main`, then either
push a tag or use the Actions tab:

```
git tag v2.0.0 && git push origin v2.0.0
```

or **Actions > Release > Run workflow**, entering `v2.0.0`. The manual
route creates the tag as part of publishing, so a release can be cut
without a local clone.

Then put the notes on the release. The workflow opens the draft with
GitHub's generated list of merged pull requests as its text, and
publishing keeps whatever text the draft has by then. The release page
is what the Update window's **What's new** link and the site's download
buttons open, so once the run's first job has opened the draft (a
minute or two in), and while the Macs build and notarize:

```
gh release edit v2.0.0 --notes-file release-notes/v2.0.0.md
```

or paste the notes into the draft on the Releases page. Running the same
command after publishing also works, but until then the release shows
the generated list.

The Release workflow:

1. refuses to start if the tag and `version.py` disagree - otherwise an
   update would install a build that reports a different version and be
   offered again forever - or if the `RELEASE_SIGNING_KEY` secret is
   missing;
2. opens a draft release (drafts are invisible to the updater, which
   reads `/releases/latest`; a failed run leaves a draft the next run
   picks up);
3. builds one `.dmg` per Mac architecture: signs the app with a Developer
   ID, proves the signed bundle can make a verified HTTPS request
   (`--netcheck`) and report its diagnostics, has Apple notarize it,
   staples the ticket into both the app and the disk image, and checks
   `spctl` accepts the result - so a release that would trip Gatekeeper
   fails the build rather than reaching anyone;
4. builds the Windows installer (`build-windows.yml`): the PyInstaller
   app, packed by Inno Setup, then installed silently on the runner, run
   once (`--netcheck`) and uninstalled again before it is attached;
5. writes `SHA256SUMS` for the three files, signs it with
   `ci/sign_sums.sh` into `SHA256SUMS.sig`, uploads both, and only then
   publishes the release.

A release that went out without a Windows build can get one afterwards:
**Actions > Windows release build > Run workflow** with its tag rebuilds
it from that tag, attaches the installer, replaces the Windows line in
`SHA256SUMS` and signs it again.

### Repository secrets

Six, under **Settings > Secrets and variables > Actions**. Two of them
are files stored as base64 text, which the workflow decodes; the release
key is not:

- `MACOS_CERT_P12` - the Developer ID Application certificate with its
  private key, exported from Keychain Access as a `.p12`, as base64:
  `base64 -i cert.p12 | pbcopy`, then paste;
- `MACOS_CERT_PASSWORD` - the password set when exporting that `.p12`;
- `NOTARY_KEY_P8` - the App Store Connect API key notarization uses,
  `AuthKey_<key id>.p8`, also as base64:
  `base64 -i AuthKey_<key id>.p8 | pbcopy`. The `.p8` is a text file
  with `BEGIN` and `END` lines, but pasted as it is it fails at
  notarization, because the workflow base64-decodes it;
- `NOTARY_KEY_ID` - that key's Key ID (the part of the file name after
  `AuthKey_`);
- `NOTARY_ISSUER_ID` - the Issuer ID (a UUID) shown with the API keys in
  App Store Connect;
- `RELEASE_SIGNING_KEY` - the private release key that signs
  `SHA256SUMS`, pasted as plain PEM text, **not** base64 - see below.

The workflow checks for all of them up front and stops immediately,
naming any that are missing.

### The release signing key

`RELEASE_SIGNING_KEY` is an Ed25519 private key in PEM form. Its public
half is built into every app as `updater.RELEASE_KEY`, and the apps
refuse any release whose `SHA256SUMS` it did not sign. On Windows, where
the installer carries no Authenticode signature, it is the only proof
that an update came from this project.

The key pair already exists. Its public half is the hex in
`RELEASE_KEY` in `updater.py`, and the private key is in the
maintainer's password manager. Setting up the secret means pasting that
stored PEM - the whole file, `BEGIN` and `END` lines included. Do not
make a new key for it.

Check the PEM before saving the secret. The workflow only checks at the
start that the secret is *there*; whether it is the *right* key is
checked by `ci/sign_sums.sh` in the last job, after both Macs have been
built and notarized, so a wrong key costs a whole release run. Save the
PEM to `release-key.pem` and print its public half, with OpenSSL 3
(macOS's own `openssl` is LibreSSL and cannot handle Ed25519 keys;
Homebrew's `openssl@3` or Git for Windows' OpenSSL can):

```
openssl pkey -in release-key.pem -pubout -outform DER | tail -c 32 | xxd -p -c 32
```

The output must equal the hex in `updater.py`. Or run the release's own
check from the repository root, with OpenSSL 3 first on the `PATH` (on
Windows, run it in Git Bash with `PYTHON=python` in front of `bash`):

```
f=$(mktemp) && echo test > "$f"
RELEASE_SIGNING_KEY="$(cat release-key.pem)" bash ci/sign_sums.sh "$f"
```

The right key ends with `signed; the app's verifier accepts it`; any
other says `RELEASE_SIGNING_KEY is not the key the app trusts`.

Once the secret is saved, check the secret itself with the **Check the
release signing key** workflow. It runs the same `ci/sign_sums.sh` on a
throwaway file in under a minute, without making a release, and prints
nothing of the key:

```
gh workflow run check-release-key.yml
```

It also runs on pull requests that touch `version.py`, `updater.py`,
`sigcheck.py` or `ci/sign_sums.sh`.

The private key **never goes in the repository**: it lives in the
maintainer's password manager and in the repository secret, nowhere
else; delete the `.pem` file once the secret is saved. At release time
`ci/sign_sums.sh` checks, before anything is uploaded, that the
secret's public half is `updater.RELEASE_KEY` and that the app's own
verifier (`sigcheck.py`) accepts the signature, so a wrong key fails
the release instead of reaching users.

Keep the password-manager copy safe: an installed app trusts only the
key it was built with. Only if the private key is lost, make a new
pair:

```
openssl genpkey -algorithm ed25519 -out release-key.pem
```

Print its public half with the command above, replace the hex in
`RELEASE_KEY` in `updater.py` with it, and commit that before the next
release's version bump; then store the new PEM and update the secret.
Every copy already installed trusts only the old key, so it will refuse
every release signed with the new one: each user has to install once by
hand, and that release's notes should say so.

### Why the certificate roots matter

All update traffic runs on top of `netcerts`, which is where the CA roots
come from. A PyInstaller build carries no certificate store of its own:
OpenSSL looks for roots where the *build* machine kept them, and macOS
keeps its own in the system keychain, which OpenSSL never reads. So the
roots are taken from OpenSSL if it has any, otherwise from the bundled
`certifi`, otherwise by exporting the system keychain - and if a request
still fails to verify, the remaining sources are added and it is retried
once, which is what lets the app work on a network that inspects TLS
with a root only the keychain knows about.

A test cannot catch this from a source checkout, because there the
system's own certificates are present and everything passes. So CI runs
`StreamSync --netcheck` against the built Mac bundle and the installed
Windows app, and fails if it cannot complete a verified request to
GitHub. You can run the same command against your own copy.

## Credits

StreamSync stands on excellent open software: [VLC / libvlc](https://www.videolan.org)
(VideoLAN) for playback, [BlackHole](https://existential.audio/blackhole/)
by Existential Audio (GPL-3.0) as the macOS audio capture backbone,
ffmpeg (via imageio-ffmpeg) for decoding, the soundcard, numpy and
websockets Python libraries, and [Inno Setup](https://jrsoftware.org/isinfo.php)
for the Windows installer. StreamSync downloads BlackHole's installer
from its developer's official server at setup time and never
redistributes it.
