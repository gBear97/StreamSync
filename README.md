# StreamSync

Watch a live stream for the streamer's commentary audio, while your own
high-res copy of the film plays the picture - kept in sync.

StreamSync listens to a few seconds of your system audio (the stream's
sound, commentary and all), finds that exact moment in your local file,
and seeks VLC to it - compensating for the seconds the search itself
takes. The stream can stay **minimized**; only its audio needs to play.
A screen-capture video matcher is included as a fallback, with facecam
ignore-zones for streamers who overlay a camera on the film.

## Requirements

- Windows, Python 3.9+ (64-bit)
- VLC 64-bit installed
- `pip install -r requirements.txt`

## Run

```
python streamsync.py
```

Or build a double-clickable exe (no console window):

```
powershell -File build_exe.ps1     # produces dist\StreamSync\StreamSync.exe
```

### First-run check

On startup StreamSync verifies its dependencies: VLC (64-bit), the
bundled ffmpeg, and - when running from source - the Python packages.
If everything is present the check is invisible. If something is
missing, a fix-it dialog appears with one-click installs:

- **Python packages** - runs pip for you and re-checks.
- **VLC** - installs through **winget**, Windows' built-in package
  manager: the download is hash-verified by Microsoft's repository and
  the installer is VideoLAN's own signed one. Windows shows a normal
  admin (UAC) prompt during install. If winget isn't available, a button
  opens the official videolan.org download page instead, and the dialog
  re-checks automatically once you've installed it.

Each fix shows its progress in the dialog and confirms with a green
check when done; the Start button unlocks once everything passes.
(`python app.py` still works and skips the gate - handy for development.)

## Quick start

1. **Video file...** - pick your local copy of the film.
2. Pick a **Player**:
   - *Embedded* (default): video renders in StreamSync's own window with
     millisecond-precise seeking. Subtitle picker included.
   - *External VLC app*: your normal VLC opens and StreamSync drives it
     remotely. Its remote interface only seeks on whole seconds, so
     StreamSync times each seek to fire at exactly the right instant, and
     does sub-second nudges as brief smooth speed pulses. Use VLC's own
     menus for subtitles/audio tracks in this mode.
3. Leave **Sync by: Audio** selected. Check the **Listen on** device is
   the one the stream plays through.
4. Type a rough **Position hint** (e.g. `1:23:00`) and hit
   **Sync to stream**. It records ~6 s of stream audio, finds the moment,
   and starts playback there. Local audio is muted by default - the
   stream provides the sound.
5. Fine-tune with the **nudge buttons** until motion matches the voice
   track. Nudges accumulate into an offset reapplied on every later sync.

## Auto mode

Tick **Auto re-sync (audio) every N s** and StreamSync quietly re-checks
sync in the background:

- Each check records 4 s of audio and scans ±45 s around where the film
  *should* be - roughly one second of CPU work per check, then it sleeps.
  Raise the interval if you want it even lighter.
- Drift beyond 0.35 s is corrected automatically.
- With **follow stream pauses** on: when the film's audio disappears from
  the stream for two consecutive checks (streamer paused, or is talking
  over a black screen), your copy pauses too. When the film's audio comes
  back, it resyncs and resumes automatically. Fully hands-free.

## See the streamer during pauses

With **Show stream window while paused** on, a detected pause (auto mode,
or you hitting pause yourself) restores the stream's browser window so
you see the facecam while the streamer talks, and tucks your film window
out of the way. When the film starts again, the browser re-minimizes and
the film comes back (fullscreen restored if it was fullscreen).

Pick the browser window under **Stream window** (Refresh lists open
windows; anything with Twitch/Kick in the title is auto-picked). Note:
"piping" the stream's video into the app isn't possible while the window
is minimized - Windows doesn't render minimized windows - so restoring
the real window is both the cheapest and the only way to show it.

Manual **Resync** (or Ctrl+Alt+R) is always available and searches around
the current position - mostly backwards, since a pause leaves your copy
ahead.

## Hosted watch sessions (watch party)

For streamers who don't want any film audio or video on their stream at
all: the host streams **only themselves**, and every viewer plays their
own copy of the film, kept in sync by StreamSync. Nothing copyrighted
ever crosses the network - the entire protocol is positions, UTC
timestamps and non-invertible fingerprint hashes (sign-bit signatures
from which audio cannot be reconstructed; a whole film's fingerprint is
~56 KB of hashes).

**Host** (Watch party > Host a session...): pick your film, a relay
server, and how StreamSync should track the film's position - by
*listening* to whatever player you're using (play it in anything), or
from StreamSync's own player. Your mic is fingerprinted continuously so
viewers can measure their personal stream delay against your voice.
Share the session code with your viewers (password optional).

**Viewers** (Watch party > Join a session...): pick your copy, enter the
code. Your file is verified against the host's hashes first - different
bitrates and releases are fine (a constant offset is detected and
absorbed); a different cut or the wrong film is refused with a clear
message rather than synced wrong. Then playback follows the host's
timeline, delayed by *your* measured stream delay, so the film on your
screen matches the commentary in your ears - and when the host pauses,
your copy pauses at the moment the commentary about it reaches you.

Under the hood: both ends sync to internet time (NTP) so "position X at
time T" means the same instant everywhere - machine clocks are often
off by hundreds of milliseconds (this machine: 404 ms). The host
broadcasts a delay hint that applies immediately; each viewer's client
then refines it automatically by finding the host's voice fingerprints
in their own incoming stream audio.

**Relay server**: `python relay_server.py --port 8765` on any machine
both sides can reach (a $5 VPS or small AWS instance serves thousands of
viewers - traffic is a few tiny messages per second per room). Point
both clients at `ws://your-server:8765`.

## Video sync (experimental fallback)

If a streamer ducks the film audio so low that audio matching reports
weak scores, switch **Sync by: Video screen-capture (experimental)**:

- **Capture region...** - drag a box over the stream's video area
  (the stream must be visible on screen for this method).
- **Edge cases** menu: ignore a facecam corner (or drag a custom ignore
  zone), and a toggle for mirror-flipped streams. Black bars are cropped
  automatically.

## Global hotkeys

| Keys | Action |
| --- | --- |
| Ctrl+Alt+S | Sync to stream |
| Ctrl+Alt+R | Resync around current position |
| Ctrl+Alt+P | Play / pause local video |
| Ctrl+Alt+Left / Right | Nudge -0.1 s / +0.1 s |

Embedded video window: **F11**/Fullscreen button toggles fullscreen,
**Esc** exits, **Space** pauses.

## How it works

- **Audio**: 26 log-spaced band energies every 16 ms, level-normalized,
  matched by normalized cross-correlation against the file's audio track
  (decoded by a bundled ffmpeg - audio-only decode is fast, so even
  whole-file scans take well under a minute). Commentary over the film is
  fine: in testing, the matcher stayed millisecond-accurate with talk
  noise 1.2x louder than the film audio. Every match reports a score and
  a peak-sharpness value (z); weak matches are flagged rather than
  trusted.
- **Video**: small grayscale thumbnails, black bars auto-cropped, facecam
  zones masked out, compared by zero-normalized cross-correlation. A
  burst of 4 frames is matched as a sequence at 12 fps (~83 ms
  resolution). Windows over 12 minutes use a keyframes-only prepass.
- **Timing**: the matched moment is corrected by exactly how long the
  capture + search took (the stream kept playing meanwhile), plus your
  accumulated nudge offset.

## Tips & troubleshooting

### When something fails, ask the app why

StreamSync keeps a log at `~/Library/Logs/StreamSync/streamsync.log`
(macOS) and can describe its own environment:

```
/Applications/StreamSync.app/Contents/MacOS/StreamSync --diagnose
```

That prints which libvlc it found, the architecture it was built for,
who signed it, whether this process can actually load it, and what macOS
said if it could not. **Advanced > Diagnostics...** shows the same report
with a button that copies it. The report is also written to the log
automatically whenever the player fails to start, so the evidence exists
without anyone having to reproduce the failure on purpose.

This matters most for one case that used to be invisible: a VLC built
for a different processor than StreamSync. An Intel VLC will not load
into an Apple Silicon build (or the reverse), and the old error said
"Install VLC from videolan.org" - advice that cannot work, because VLC
was already installed. The report names the mismatch instead.


- Audio sync assumes the stream is the *loudest thing* on that playback
  device. Pause your music first, or route the stream to its own device
  and pick it under **Listen on**.
- Quiet dialog scenes under loud commentary are the hard case. If a match
  comes back weak, wait for music/action, or narrow the search window.
- 4K HEVC HDR / Dolby Vision copies: audio sync doesn't care about the
  video format at all (it never touches the video track), so prefer it
  there - video-method scans of HEVC are slower and HDR tone curves can
  lower match confidence. For HDR playback quality itself, VLC tonemaps;
  use External VLC mode if you have a tuned VLC setup for HDR.
- Streams run 5-30 s behind live; that's irrelevant to sync (you're
  syncing to what you *hear*), but it means your copy will be "behind
  live" too - as it should be.
- Settings (file, region, devices, mode, auto options) persist in
  `~/.streamsync.json`.
- If global hotkeys don't register, run the terminal as administrator
  once, or just use the buttons.

## Updates

> **1.0.0 and 1.0.1 cannot update themselves.** Those builds shipped
> without any CA certificates, so every HTTPS call out of them failed to
> verify and the update check never reached GitHub. If you are on one of
> them, download 1.0.2 or later from the
> [releases page](https://github.com/gBear97/StreamSync/releases) once by
> hand; updates work from there on.

StreamSync checks GitHub for a newer release when it starts, and quietly
does nothing if there isn't one or the machine is offline. **Advanced >
Check for Updates...** asks on demand. When something newer exists it
offers to install it: the disk image is downloaded over HTTPS, checked
against the `SHA256SUMS` published in the same release, and unpacked
beside the installed copy - where its code signature must prove intact
and issued to StreamSync's own Developer ID team before anything is
installed. Only then is the swap handed to a helper that runs after the
app quits and puts the old bundle back if the move fails. A copy running
from a source checkout never replaces itself; it tells you to `git pull`.

All of that runs on top of `netcerts`, which is where the CA roots come
from. It matters because a PyInstaller build carries no certificate store
of its own: OpenSSL looks for roots where the *build* machine kept them,
and macOS keeps its own in the system keychain, which OpenSSL never
reads. So the roots are taken from OpenSSL if it has any, otherwise from
the bundled `certifi`, otherwise by exporting the system keychain - and
if a request still fails to verify, the remaining sources are added and
it is retried once, which is what lets the app work on a network that
inspects TLS with a root only the keychain knows about.

A test cannot catch this from a source checkout, because there the
system's own certificates are present and everything passes. So CI runs
`StreamSync.app/Contents/MacOS/StreamSync --netcheck` against the built
bundle - on release builds, against the *signed* bundle, before spending
a notarization on it - and fails if it cannot complete a verified request
to GitHub. You can run the same command against your own copy.

To cut a release, bump `__version__` in `version.py`, commit it, then
either push a tag or use the Actions tab:

```
git tag v1.0.1 && git push origin v1.0.1
```

or **Actions > Release > Run workflow**, entering `v1.0.1`. The manual
route creates the tag as part of publishing, so a release can be cut
without a local clone.

The Release workflow refuses to build if the tag and `version.py`
disagree - otherwise an update would install a build that reports a
different version and be offered again forever. It signs the app with a
Developer ID, has Apple notarize it, staples the ticket into both the app
and the disk image, and checks `spctl` accepts the result before
publishing - so a release that would trip Gatekeeper fails the build
rather than reaching anyone. It publishes one `.dmg` per architecture
plus the `SHA256SUMS` the updater verifies against.

Signing needs five repository secrets - `MACOS_CERT_P12`,
`MACOS_CERT_PASSWORD`, `NOTARY_KEY_P8`, `NOTARY_KEY_ID` and
`NOTARY_ISSUER_ID`. The workflow checks for all five up front and stops
immediately naming any that are missing.

## macOS

The same engine with a Mac-shaped shell: a minimal main window (film,
Sync/Resync, hint, nudges, status) and everything else in the menu bar -
Sync, Playback and Advanced menus. Start it the same way:

```
python3 streamsync.py        # dependency gate, then the app
sh build_app_mac.sh          # optional: build StreamSync.app (run on the Mac)
```

No Mac at hand to build on? The **Build macOS app** GitHub Actions
workflow compiles `StreamSync.app` on GitHub's macOS machines - for both
Apple Silicon (`arm64`) and Intel (`x86_64`) - on every push to `main`
or on demand (Actions tab > Build macOS app > Run workflow). Each run
attaches a disk image named for the build - e.g.
`StreamSync-1.0.0-arm64-129a805.dmg`, the version from `version.py` plus
the commit it came from, so two downloads never collide in your Downloads
folder. Grab it from the run page, unzip the artifact GitHub wraps it in,
open the `.dmg`, and drag StreamSync.app to Applications. The running
app shows its version in the window title, and Finder's Get Info reads it
from the bundle. These per-commit builds are **not signed** - only tagged
releases are - so clear the download quarantine once (`xattr -dr
com.apple.quarantine /Applications/StreamSync.app`) and use right-click >
**Open** the first time. For an ordinary install take a
[release](https://github.com/gBear97/StreamSync/releases) instead: those
are signed and notarized, and just open. VLC still needs to be installed
on the Mac that runs it.

### One-time macOS setup

1. **VLC** - the first-run gate installs it via Homebrew or points you at
   videolan.org.
2. **BlackHole 2ch** (needed for audio sync) - macOS has no built-in way
   to record "what's playing", so audio is captured through this free,
   signed virtual audio driver. The gate installs it with **one click**:
   it downloads the developer's ~100 KB signed installer from their
   official URL, verifies the sha256 against Homebrew's independently
   published checksum, and hands it to Apple's own installer (which also
   verifies the signature/notarization) - you just enter your password.
   Then the one manual step: hit **Open Audio MIDI Setup**, click **+ >
   Create Multi-Output Device**, tick your speakers *and* BlackHole 2ch,
   and set it as the sound output. Pick *BlackHole* under Advanced >
   Listen On in StreamSync.
3. **Permissions** - macOS will prompt as features are first used:
   *Microphone* (recording from BlackHole), *Screen Recording* (only for
   the experimental video method), *Automation/System Events* (only for
   the show-stream-while-paused swap).

### Platform differences

- The "built-in" player is a VLC-drawn video window (Tk can't host VLC
  on macOS); millisecond sync control is identical. External VLC.app
  mode works the same as on Windows.
- The facecam swap works at the app level: the browser app comes forward
  during pauses and hides on resume (pick it under Advanced > Stream App).
- No global hotkeys (macOS requires root for that) - use the menu
  accelerators (Cmd-S sync, Cmd-R resync, Cmd-P pause) while the app is
  focused, or the buttons.
- Multi-monitor region capture is primary-display only for now.

## Antivirus notes

- StreamSync never downloads and runs executables itself - VLC installs
  go through winget (verified) or your own browser. That avoids the
  classic "program fetched an exe and ran it" heuristic.
- The packaged `StreamSync.exe` is an unsigned PyInstaller build. On
  *your* machine (where you built it) Defender is normally fine. If you
  copy it to another PC, SmartScreen may show "unknown publisher" -
  expected for any unsigned exe; "More info -> Run anyway" or run from
  source instead.
- The optional `keyboard` package installs a global keyboard hook for
  the hotkeys. Some aggressive AV/anticheat tools flag global hooks as
  keylogger-like. If yours complains, uninstall `keyboard` - the app
  runs fine with buttons only.
- Screen capture and loopback audio recording use the same standard
  Windows APIs as OBS; they don't normally trigger anything.

## Credits

StreamSync stands on excellent open software: [VLC / libvlc](https://www.videolan.org)
(VideoLAN) for playback, [BlackHole](https://existential.audio/blackhole/)
by Existential Audio (GPL-3.0) as the macOS audio capture backbone,
ffmpeg (via imageio-ffmpeg) for decoding, and the soundcard, mss, numpy
and Pillow Python libraries. StreamSync downloads BlackHole's installer
from its developer's official server at setup time and never
redistributes it.

## Verify the matchers

```
python test_matcher.py        # video: synthetic clip, expects <0.35 s error
python test_audio_matcher.py  # audio: film-like audio + loud commentary noise
```
