# StreamSync

StreamSync plays your own copy of a film in step with a live stream that carries the film's sound. It listens to the system audio, finds that moment in the local file and seeks VLC to it. It's Python, and GitHub Actions build the Windows and macOS apps (`.github/workflows/`). **This repository is public.**

The current state, open PRs and next steps are in `docs/STATE.md`. Read it first. When things change, replace the stale entries there; don't append.

## Standing rules

- Commits, pushes, PR edits, merges and releases wait for the owner's word.
- Keep subagents out of the working tree: every agent brief must forbid edits to the repo. A judge agent edited it uninvited on 2026-11-12.
- Public repo: no personal names, hosts, device IDs or machine paths in anything committed.
- Before a live test, back up `~/.streamsync.json`, and restore it byte for byte afterwards. Quit the app with `osascript -e 'tell application id "StreamSync" to quit'` so it saves its settings.
- Only `v*` tags start `release.yml`. Old unmerged branches are kept as `archive/<branch>` tags.

## Pitfalls learned the hard way

- **zsh doesn't word-split** a variable that holds a command and its arguments (`H="python3 path"; $H check`, `set -- $spec`, `$ADB -s X`). It fails, or runs silently with the wrong arguments; one scoring run scored only "live" this way. Call the program directly, or use an array or a function.
- **numpy:** the system `python3` doesn't have it. Use `.venv/bin/python` for tests and harness scripts. A silent failure in a command chain once gave a wrong `t_first`.
- **Heavy jobs:** run them with `nice -n 10`, at most two replays at once. Never use `taskpolicy -b`, which pins jobs to the E-cores (10–20× slower).
- **Waits:** a foreground `sleep` over about 20 s is blocked, and tools time out at 300 s. Run the wait in the background as a loop that compares epoch seconds (`date +%s`), not `%H%M%S`.
- **Browsers:** the Chrome tools can attach to the PC's browser. Call `list_connected_browsers` and select the macOS one before anything that plays sound.
- **VOD pages:** they refuse JS `play()` until one real click. After that, seek, pause and play by JS, and schedule stops with in-page `setTimeout` (`Date.now()` plus `currentTime`).
- **Audio timing:** an audio element's `playing` event comes 35–45 ms before the sound (0.34 s at first start). Take timing anchors from the once-a-second clock readings.
- **macOS permissions:** Claude's shell can't read the user TCC.db. Use `/usr/bin/log show --info --predicate 'process == "tccd"'` and grep `Update Access Record` or `AUTHREQ_RESULT`.
  - zsh's `log` is a builtin, so give the full path.
  - Without `--info` the grant lines don't appear.
- **PR bodies:** `gh pr view` returns them with CRLF. Compare them ignoring `\r` and outer whitespace.
- **Local-only work:** `git log --branches --not --remotes` says there's none when the remote refs are stale, so run `git fetch --prune` first. To prove a squash merge lost nothing, check that `git merge-tree --write-tree <branch tip> <main parent>` equals the squash commit's tree.

## Live tests on the Mac

- **The app's log:** `~/Library/Logs/StreamSync/streamsync.log`. The sync and `swap:` lines show whether the permissions worked. The sync error is the log's `drift`; `offset` is a setting, not a measurement.
- **Recorder:** `/Applications/StreamSync.app/Contents/MacOS/sstap --exclude-pid <app pid> --out <file> --seconds N`.
- **Test builds:** they install as `/Applications/StreamSync Test.app` (bundle `StreamSync.Test`) with `~/.cache/streamsync-bench/scripts/install_test_app.sh`, which is kept outside git.
- **Harnesses and recorded nights:** in `~/.cache/streamsync-bench/`; `docs/STATE.md` lists them.
