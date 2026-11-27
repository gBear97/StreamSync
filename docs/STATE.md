# StreamSync: current state

Last rewritten 2026-11-26 from the wrap-ups of the Mac's StreamSync sessions, and corrected the same day against GitHub. Replace stale entries; don't append a log. This repository is public: no personal names, hosts, device IDs or machine paths here.

## Releases and branches

- **Released:** 2.0.11 (2026-11-11), on both machines. Nothing has been released since.
- **`main`:** holds the whole "Pause with the stream" stack, unreleased. On top of 2.0.11 it has:
  - #84, the centre channel for 5.1 films (`b9357f0`, merged 2026-11-14);
  - #90 (`e14f53e`, merged 2026-11-26), one squash of the branch `claude/watch-holds-its-place` at `8ed685f`. Its tree equals `8ed685f`, so it carries the code of all five PRs below;
  - the docs commit that added this page and `CLAUDE.md`.
- **The five stacked PRs were closed without merging on 2026-11-26,** at the owner's word, because their code is already in `main` through #90. Each carries a comment that says so, and all five branches are kept. Each was based on the one before.

  | PR | Branch | What it adds |
  |---|---|---|
  | #85 | `claude/wide-search-stop-inside-pause` (base `main`) | a wide search skips a recording that ends in silence |
  | #86 | `claude/watch-while-settling` | listen for a silent stream while the film settles |
  | #87 | `claude/wide-search-needs-sound` | a wide search waits for half a second of unbroken sound |
  | #88 | `claude/wide-search-by-centre` | a wide search looks in the film's centre channel once the copies differ |
  | #89 | `claude/watch-holds-its-place` (`8ed685f`, still checked out on the Mac) | the film holds its place through displaced sound, but not for 180 s after a real stop, seek or sync |

- **Old work:** ten unmerged branches from the PC are kept as `archive/<branch>` tags. Only `v*` tags start `release.yml`.

## Where the live testing stands

- **2026-11-25, early:** #89 ran live on the Mac against a stand-in stream: the 2026-11-12 tap recording, served from 127.0.0.1, because the VOD's host answered 502. The results are in #89's description.
  - After a real stop the film is not held, as intended (it let go after 11 s at 54:29).
  - The hold works once the film has been in step for 3 minutes.
  - The film has not been run against the VOD itself or a real live stream yet.
- **2026-11-25, evening:** test builds of `8ed685f` were made (workflow runs 45752902500 for macOS and 45752905568 for the tests and Windows; both green).
  - **Mac:** installed as `/Applications/StreamSync Test.app` (bundle `StreamSync.Test`, ad-hoc signed; it reports 2.0.11).
  - **PC:** sent as handoff `2026-11-25-hold-test-build-win` (delivered and verified). It isn't installed until the PC session runs `/inbox`.
  - **The real live test has not happened:** the Mac's log shows the test app opened twice, with no film, sync, pause or swap.
- **macOS permissions for the test app:** only Screen Recording is on record (granted 2026-11-25 20:36). There's no audio-capture or System Events (Apple Events) record, so the swap will still prompt. Whether Screen Recording covers the `sstap` tap is unproven until a real sync.
- **"Keep in sync automatically"** was off in the last saved config. It was on in the early 11/25 run.
- The VOD host answered 200 again on the evening of 11/25 (front page only; playback unchecked).

## Next steps

1. **PC:** run `/inbox` to install the `8ed685f` test build.
2. **Mac:** open StreamSync Test, open the film, sync against the sound in the browser, pause once by hand, and allow the System Events prompt. Then read `~/Library/Logs/StreamSync/streamsync.log` for the sync and `swap:` lines to confirm both permissions.
3. **After that live run:** score it, fetch the PC's reply handoff, then decide whether to release 2.0.12 from `main`.
4. **To rerun against the VOD** once it plays: the stop schedule is `~/.cache/streamsync-bench/live-2026-11-25-hold-fix/live/scripted_stops.json`.

## Known problems, not fixed

- A real stop inside a displaced stretch still brings the film back on the displaced sound. Untried idea: measure how long a digitally silent stop lasts.
- At 34:03 in *Following*, auto mode moved the film −0.29 s and the watcher moved it back +0.29 s nine seconds later. All three versions do this in replay. Not investigated.
- From the #84 work, not checked against #85–#89:
  - Near-silent captures (under 8 live frames) fall back to uniform voice weights. That caused both of 2.0.11's voice restarts.
  - The comment on `WATCH_RESUME_REACH` says no voice look stood 0.30 clear. The voice test found 121 of 147k that did.
  - Voice-driven false drift corrections: 0.24 % of voiced stops in mix mode, 0.54 % in centre mode.
- On *Carol*, the end of stream is noticed in 1.8–1.9 s, against 1.2–1.3 s, when the streamer talks over it. The owner accepted this for the bigger gain. It has never been measured live on a matched-mix night.
- The voice test (`voicetest/` in the harness) finished only *Following* with real voice (350 of 420). *Carol* and the speech corpus never ran.
- From the PC's live test of #84 (traced 2026-11-22):
  - An unexplained −1.433 s correction at film 43:46, where the stream is in place. Probably a close-look resume landed off.
  - Stop 4's resume came 16.5 s late. Not traced.
  - Not fixed: a close-look resume into a stop (the 5 s + 3 s pair), and a streamer talking over a stopped film.
- **Open design question:** once the centre channel is on, should auto mode prefer the dialogue? #88 makes the wide search look in the centre. None of the wrap-ups says auto was changed.

## Findings worth keeping

- **The stream's copy of the film sits off the file in stretches.** This is why auto makes large corrections (1.4–1.9 s); they are not a regression from #84. 2.0.11 made the same jumps by pausing and running a wide search, so its log showed no auto correction.
  - *Following* 38:17–38:47: every channel is 1.93 s off, so following the sound is right.
  - 25:58–28:40: only the front L+R music is 1.46 s off and the dialogue is in place, so following is wrong for the dialogue.
- **Sync holds under a full GPU load on the PC** (2026-11-08, *Carol*, test build 3; the Mac was the unloaded control). With the card at a median 97 %, 95 % of sync errors stayed within 9 ms before and under load (worst 16 and 11 ms). There were no corrections, the match confidence was the same, and resumes were no slower.
  - The PC-only short pauses (3 in 48 min before, 6 in 55 min under load) don't show an effect either way.
  - To settle it: toggle the load every 15 minutes during one film.
  - Data: `~/.cache/streamsync-bench/gpu-load-2026-11-08/`.

## Decisions behind #84 (the centre channel) and why

- **A per-film latch.** Playing looks are mix-only, as in 2.0.11, until the centre beats the mix by more than `WATCH_CENTRE_LEAD` (0.15 ncc) on over 5 % of the last 800 playing looks. From then on they take max(mix, centre).
  - Counting looks where the centre leads separates the films: *Carol* peaks at 0.4 %, *Following* sits at 8–13 %, and a voice alone gives about 1 %.
  - Mix-miss shares separate them only 2×, and a voice alone drives those to 70–80 %.
- **"Missing" stays the mix's call** unless the centre hears the film. Otherwise max() delays the quick pause (*Following*'s 2.10 s stop by +0.1 s, *Carol*'s 1.1 s stall by +0.3 s).
- **Resume looks, and the exit from Play by hand, stay mix-only.** With the centre, voice restarted the film more often in the paired voice test.
- **Drift is judged by the centre**, and skipped on blocks with no centre. Judging it by whichever sound won made it bounce (30–31 corrections against 16–22).
- **Ruled out:**
  - the centre alone (7× the false pauses on *Carol*);
  - blends with L+R;
  - max() without the latch;
  - an adaptive unheard run of 12 on mismatched copies (overran the film's end by 13 s);
  - a 0.15 shift gate and a 0.25 clear gate;
  - a 0.25 or 0.30 clear rule (a straight trade);
  - auto-mode confirm or decline guards (within the noise; "decline" never fired).
- **From the 2026-11-12 VOD replay of 2.0.11**, ruled out as causes of its false pauses: the streamer's voice level, the film's quietness, the 5.1 downmix, and the VOD's audio quality. One agent scored that run, and its numbers were never re-checked.

## Data and tools outside the repository (keep)

All under `~/.cache/streamsync-bench/`:

- `centre-fix/harness/`: the replay harness (read its `README.md`).
- `live-2026-11-12/`: the third scored dataset (about 1 GB). Its outputs are in `analysis/`; the `REPORT_1112.md` an agent claimed to write does not exist.
- `live-2026-11-25-hold-fix/`: the stand-in run's `REPORT.md`, plus `standin/`, which rebuilds the stand-in stream.
- `scripts/install_test_app.sh`: installs a downloaded Mac test build as StreamSync Test. It was copied here on 2026-11-26 from a session scratchpad that will be emptied. Point its `S=` line at the folder that holds the downloaded build before running it.
- `.claude/handoff-centre-channel-matching.md`: the git-excluded handoff that started #84. It's finished; it's kept for its reasoning.

## Conflicts

None open. Two sessions described #84's playing looks differently: one said plain max(mix, centre), the other a per-film latch. The code has the latch (`WATCH_CENTRE_LEAD` in `controller.py`).
