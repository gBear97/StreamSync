"""Which mixin owns each method the others call, as data, and a check of it.

The App class is put together from mixins that are written separately
(ui/shell.py), and Python resolves a name two of them define by the method
order, without a word. So every method one mixin calls on another is fixed
here - its owner module, its signature and what it does - and problems()
reads the ui/ tree with ast (no imports, no Tk, no display) to check that:

- every name below is defined exactly once across the App's classes, in
  its owner (module-level names: at module level in their owner);
- no name at all is defined in two of the App's classes - the adapters,
  which override PlatformAdapter by design, count as one - and none twice
  in one class;
- each signature matches, parameter names and literal defaults included;
- the shared state each owner creates (STATE) is set by its owner;
- both platform adapters fill in every PlatformAdapter method that has no
  default and every constant left empty there, with the base's
  signatures;
- every queue kind in Shell.HANDLERS names a method something defines,
  and MainWindow.VIEWS lists exactly the main window's view methods.

An entry is "signature; what it does". The signature is "(params) -> what
it returns" for a function or method (params without self), "property ->
..." for a property, "class" for a class and "class attribute" for a value
in a class body. Every method runs on the Tk thread unless it says
"thread". `python -m ui.contract` prints the problems, if any.
"""

import ast
from pathlib import Path

# The App's classes: one mixin per module, and the platform side
MIXINS = {
    "ui/shell.py": "Shell",
    "ui/mainwin.py": "MainWindow",
    "ui/video.py": "FilmWindow",
    "ui/update.py": "UpdateFlow",
}
ADAPTERS = {
    "ui/adapter.py": "PlatformAdapter",
    "ui/platform_win.py": "Adapter",
    "ui/platform_mac.py": "Adapter",
}

CONTRACT = {
    "ui/shell.py": {
        # module level
        "create_app": "(root, platform=None) -> App; builds the app on root. Tests pass the "
                      "platform module (test_macswap runs platform_mac on every OS)",
        "app_class": "(platform) -> type; the App class for a platform module: (platform.Adapter, "
                     "FilmWindow, MainWindow, UpdateFlow, Shell)",
        "default_platform": "() -> module; ui.platform_mac on macOS, else ui.platform_win (static "
                            "imports, so PyInstaller finds both)",
        "main": "(); the entry point: prepare_process, the selftest's config, the Tk root and its "
                "hooks, create_app, _present_root, mainloop, then os._exit once a close completed "
                "(a selftest exits 1 when a Tk callback raised)",
        "_selftest": "() -> bool; '--selftest' in sys.argv",
        "_selftest_config": "(); points controller.CONFIG_PATH into a new temporary directory; "
                            "runs before create_app",
        # construction
        "__init__": "(root); builds the whole app, in the order the other mixins rely on: shared "
                    "state, _init_platform, _init_updates, the theme, _init_root, every Tk "
                    "variable, the film window, its menu and bar, the main window, keys, the "
                    "player, ctl, the config, devices, ctl.start, _start_platform, the loops",
        "_init_theme": "(); theme.init(self._ui_scale(), self._ui_font_family()), before any "
                       "widget",
        "HANDLERS": "class attribute; queue kind -> method name, looked up on the instance when "
                    "dispatched",
        # players and the film
        "player": "property -> the player in use: ctl.player",
        "external": "property -> the VLC app's player, or None: ctl.external",
        "_choose_file": "(); the file dialog. For the built-in player _show_video_window() runs "
                        "before ctl.load_file; a VLCError shows an error and changes nothing. "
                        "Then _film_loaded(path), the status line and a save",
        "_film_loaded": "(path); a film is in: _film_length None, _sync_state 'none', _show_film "
                        "and _show_main_state, then _party.refresh() if the dialog is "
                        "open; a thread probes the length and posts "
                        "('film_info', path, seconds)",
        "_on_film_info": "(path, seconds); queue: ignores a stale path, else sets _film_length "
                         "and calls _show_film",
        "_apply_player_choice": "(); Settings changed player_var: switch players; a failed "
                                "switch resets player_var",
        # syncing
        "_ready": "() -> bool; False, after saying 'Open a film first.', when there is no film",
        "_sync": "(resync=False, whole=False) -> bool; True only if a sync started",
        "_resync": "() -> bool; self._sync(resync=True)",
        "_set_busy": "(on); the state of sync_btn, resync_btn, _fs_resync and _fs_full; on also "
                     "sets _sync_state 'syncing' and shows it",
        "_on_busy_off": "(); queue: _set_busy(False), _fs_sync_done(), then 'failed' if the "
                        "state is still 'syncing'",
        "_on_auto_toggle": "(); auto_var, follow_var or interval_var changed: applied to ctl and "
                           "saved, then _show_auto_sub()",
        "_on_auto_off": "(text); queue: auto mode gave up: auto_var off as a manual switch-off "
                        "would, then the text as the status",
        "_on_synced": "(info); queue: a sync result {match, at, trusted, applied, source} for "
                      "the sync-state row, the status line and the bar",
        # playback
        "_nudge": "(delta); self._show_offset(self.ctl.nudge(delta))",
        "_reset_offset": "(); ctl.reset_offset(), then _show_offset(0.0)",
        "_toggle_pause": "(); pause or play, then the stream swap that goes with it",
        "_toggle_mute": "(); flips mute_var, then _on_mute_toggle()",
        "_on_mute_toggle": "(); mute_var to ctl.set_mute, then _show_mute()",
        "_toggle_fullscreen": "(); no film: nothing. Else _fullscreen_from_window(), then "
                              "_set_fullscreen(not self.fullscreen) for the built-in player or "
                              "external.fullscreen_toggle() for the VLC app",
        "_seek_by": "(delta); from player.time() (None: nothing), kept inside the film; "
                    "player.seek, then _mark_moved()",
        "_can_seek": "() -> bool; the last tick read a time, and a length is known",
        "_seek_to_frac": "(frac); player.seek(frac * length), then _mark_moved(); the Timeline "
                         "has already deferred it",
        "_mark_moved": "(); a synced or weak state becomes 'moved', and shows it",
        # keys
        "_bind_keys": "(toplevel); binds Space, Left, Right, f, F and Escape on toplevel to "
                      "_on_key",
        "_on_key": "(action, event) -> 'break' or None; action is pause, back, forward, "
                   "fullscreen or leave; None leaves a text field its key",
        "_typing": "(event) -> bool; the event's widget is a text field",
        # dialogs and the watch party
        "_open_settings": "(); lifts Settings if it is open, else builds it",
        "_open_watch_party": "(); lifts Watch party if it is open, else builds it",
        "_session_running": "() -> bool; ctl.session_running()",
        "_session_changed": "(); _party_live from ctl, _show_party_live, then _party.refresh() if "
                            "the dialog is open",
        "_on_session_text": "(text); queue: _session_text, the status line, and "
                            "_party.set_session_text(text) if the dialog is open",
        "_leave_session": "(); ctl.leave(), _session_changed(), then the status 'Left the "
                          "session.'",
        # hosting through this computer (a Cloudflare quick tunnel)
        "_host_here": "(params, exe, download); ctl.begin_hosting() here, so Cancel finds it at "
                      "once, then a worker thread runs ctl.open_tunnel and posts "
                      "('hosting', text) as it goes, then ('hosting_ready', local, public) or "
                      "('hosting_failed', reason, manual); params are ctl.host's arguments "
                      "after relay_url",
        "_hosting_now": "() -> bool; the tunnel is opening: ctl.hosting set, no session yet",
        "_on_hosting": "(text); queue: the status line and _party.set_host_note(text), unless "
                       "the start was cancelled",
        "_on_hosting_ready": "(local, public); queue: ctl.host(local, remember_relay=False) "
                             "with the saved params, ctl.public_relay = public, "
                             "_session_changed and a save - unless cancelled meanwhile",
        "_on_hosting_failed": "(reason, manual); queue: the status line and "
                              "_party.hosting_failed(reason, manual)",
        "_cancel_hosting": "(); ctl.stop_hosting(), the status line, _party.refresh()",
        "_open_cloudflared_page": "(); Cloudflare's cloudflared download page, in the browser",
        # devices and the stream list
        "_populate_audio_devices": "(); thread: lists the outputs, '(default) X' first, and "
                                   "posts ('devices', items)",
        "_apply_devices": "(items); queue: picks the device, keeps _device_items, updates "
                          "Settings if open and the FirstRun line",
        "_on_device_pick": "(_event=None); device_var to ctl.audio_device ('' for the default), "
                           "then a save",
        "_device_label": "(value) -> str; '(default) X' reads 'X (DEFAULT_DEVICE_NOTE)'; "
                         "anything else reads as itself",
        "_populate_mics": "(); thread: lists the microphones and posts ('mics', names)",
        "_on_mics": "(names); queue: _party.set_mics(names) if the dialog is open",
        "_apply_stream_list": "(items); _stream_choices = list(items), and Settings' choices if "
                              "it is open",
        # subtitles
        "_open_subs_menu": "(); the Subtitles menu popped under subs_btn (the VLC app: a status "
                           "message instead)",
        "_load_sub_file": "(); asks for a subtitle file for the built-in player",
        # plumbing
        "_set_status": "(text); status_lbl and _fs_status, and the status line shown only when "
                       "there is text",
        "_poll_queue": "(); every 80 ms: dispatches the queue through HANDLERS, then "
                       "_settle_fullscreen(); stops once the app is closing",
        "_tick_time": "(); every 700 ms: the position to _show_position, then _tick_status()",
        "_tick_status": "(); the ago label, a session that ended by itself, the film link and "
                        "the subtitle label",
        "_save_config": "(); hint, player, swap, stream_window and check_updates, through "
                        "ctl.save_config",
        "_load_config": "(); restores the film (its window shown first) and the shell's "
                        "settings, reading 1.x's OLD_STREAM_KEY once",
        "_on_close": "(); once: _closing, _close_platform(), _cancel_update(), _save_config(), "
                     "ctl.close(), _exit_hard, root.destroy()",
        "_selftest_tour": "(); opens and closes Settings, Watch party and an update offer, "
                          "stopping if the app is closing meanwhile; downloads nothing",
    },
    "ui/mainwin.py": {
        "VIEWS": "class attribute; the names of the view methods below, so tests can stub "
                 "them all",
        "_build_main": "(); both boards (FirstRun shown) and the main window's widgets; reads "
                       "nothing from ctl",
        # the views change widgets only, never ctl or the settings
        "_show_main_state": "(); FirstRun or Main, on bool(ctl.video_path)",
        "_show_film": "(name, length); the header: the film's file name, and its length in "
                      "seconds (None: not known yet) with the player in use",
        "_show_film_link": "(visible); the header's 'Show film window' link",
        "_show_sync_state": "(state, at=None) -> str; the sync-state row for none, syncing, "
                            "synced, weak, missed, failed or moved (at: the stream position "
                            "matched); returns the headline as plain text",
        "_show_ago": "(text); the sync-state row's 'N s ago' ('' for none)",
        "_show_auto_sub": "(); the line under 'Keep in sync automatically', from interval_var "
                          "and follow_var",
        "_show_position": "(t, n, playing); the timeline, both times and the play button (t "
                          "None: 0:00, the knob at 0)",
        "_preview_position": "(frac); while the timeline is dragged: the time label only",
        "_show_mute": "(); mute_btn from mute_var",
        "_show_subtitle": "(text); subs_btn reads 'Subtitles: ' + text ('Off', a track's "
                          "name, 'in VLC')",
        "_show_offset": "(offset); offset_lbl as f'{offset:+.2f} s'",
        "_show_party_live": "(on); the live dot on the Watch party button",
        "_show_listen": "(label); FirstRun's 'Listening to: ...' line, label as _device_label "
                        "gives it",
        "_show_status_line": "(visible); shows or hides status_lbl's line",
    },
    "ui/video.py": {
        "FS_BAR_IDLE": "class attribute; seconds of a resting mouse before the bar hides",
        "WheelSteps": "class; wheel deltas into volume steps",
        "WheelSteps.__init__": "(unit, sign=None, quiet=None, clock=time.monotonic)",
        "WheelSteps.feed": "(delta) -> int; the signed number of steps to take now",
        "_build_video_window": "(); the film window (withdrawn), its bindings and _wheel; ends "
                               "with update_idletasks()",
        "_build_video_menu": "(); the right-click menu (video_menu) with its Subtitles cascade "
                             "(_sub_menu)",
        "_build_fs_bar": "(); the fullscreen bar (fs_bar, withdrawn) and every _fs_* attribute",
        "_fill_sub_menu": "(menu); empties menu, then lists the subtitle tracks (the current "
                          "one ticked), a separator and 'Load subtitle file...'",
        "_fullscreen_entered": "(); the film is fullscreen now (Windows at once, the Mac once "
                               "it settles): the film takes the keyboard where the platform "
                               "asks, and the bar shows",
        "_hide_fs_bar": "(hide_pointer=False); hides the bar, and the pointer with it if asked",
        "_fs_sync_done": "(); a sync finished: the bar's resync buttons back as they were, its "
                         "hold released",
        "_step_volume": "(step); the built-in player's volume by step (0-125, never unmuting), "
                        "shown on the OSD",
        "_flash_osd": "(text, ms=900); the OSD at the film's top-right, for ms",
    },
    "ui/update.py": {
        "UpdateCancelled": "class; raised inside the download to stop it (not an OSError, so "
                           "download_verified does not wrap it)",
        "_init_updates": "(); the update state: _update_checking, _update_offer, "
                         "_update_running, _update_cancel, _update_line; no Tk",
        "_check_updates": "(quiet=True); one check at a time, on a thread, which always posts "
                          "one of update, update_none or update_err",
        "_show_update_offer": "(); opens the offer the last check found (Settings' 'Update...')",
        "_start_update": "(tag, rel); download, verify and install on a thread; everything "
                         "comes back through the queue",
        "_cancel_update": "(); asks a running download to stop (_update_cancel); _on_close "
                          "calls it",
        "_open_release_notes": "(rel); rel's html_url, else the releases page, in the browser",
        "_open_releases_page": "(); the releases page, in the browser",
        "_on_update": "(tag, rel); queue: a newer version: recorded, the Settings line set, and "
                      "UpdateDialog opened unless one is open or an update is running",
        "_on_update_none": "(version); queue: this is the latest version",
        "_on_update_err": "(text); queue: the check failed, and why",
        "_on_update_progress": "(done, total, name); queue: download progress",
        "_on_update_step": "(text); queue: an install step to show",
        "_on_update_installing": "(); queue: past the point where Cancel can stop it",
        "_on_update_ready": "(); queue: installed; closes the app through _on_close",
        "_on_update_failed": "(why); queue: the update failed and nothing was changed",
        "_on_update_cancelled": "(); queue: the download was cancelled",
    },
    "ui/dialogs.py": {
        "SettingsDialog": "class; the Settings window",
        "SettingsDialog.__init__": "(app); builds it, sets app._settings = self before "
                                   "app._present(win), fills its fields, then asks for the "
                                   "device list (and, if REFRESH_STREAMS_ON_SETTINGS, the "
                                   "stream list)",
        "SettingsDialog.set_devices": "(items); the 'Listen to' choices",
        "SettingsDialog.set_stream_choices": "(items); the stream choices, after the '' "
                                             "(Automatic) one it always has",
        "SettingsDialog.set_update_line": "(text, action); the Updates row's text, and its "
                                          "button: 'check' (Check now), 'update' (Update...) "
                                          "or None (disabled while a check runs)",
        "SettingsDialog.apply": "() -> bool; validates and applies the fields; False keeps the "
                                "dialog open on the field in error",
        "SettingsDialog.close": "(); releases the grab, destroys the window, app._settings = "
                                "None",
        "WatchPartyDialog": "class; the Watch party window",
        "WatchPartyDialog.__init__": "(app); builds it, sets app._party = self before "
                                     "app._present(win), then starts app._populate_mics(); "
                                     "no grab, and beside the main window when the "
                                     "screen has room",
        "WatchPartyDialog.refresh": "(); the idle or the running view, on "
                                    "ctl.session_running()",
        "WatchPartyDialog.refresh_code": "(); the running view's code from ctl.session.code "
                                         "('…' until there is one); writes only on a change",
        "WatchPartyDialog.set_session_text": "(text); the running view's latest session line, "
                                             "then refresh_code()",
        "WatchPartyDialog.set_mics": "(names); the microphone choices",
        "WatchPartyDialog.set_host_note": "(text); the tunnel's progress under the Host button",
        "WatchPartyDialog.hosting_failed": "(reason, manual); the reason under the Host button, "
                                           "with the way to get cloudflared by hand if manual",
        "WatchPartyDialog.close": "(); destroys the window, app._party = None",
        "UpdateDialog": "class; the Update window",
        "UpdateDialog.__init__": "(app, tag, rel, can_install, unavailable_text); the offer, "
                                 "or why this copy can't install it; sets app._update_dialog "
                                 "= self before app._present(win)",
        "UpdateDialog.show_progress": "(done, total, name); the progress view",
        "UpdateDialog.show_step": "(text); the progress view's step line",
        "UpdateDialog.set_installing": "(); Cancel disabled from here on",
        "UpdateDialog.show_failed": "(why); the failed view",
        "UpdateDialog.close": "(); releases the grab, destroys the window, "
                              "app._update_dialog = None",
    },
}

# The shared state each owner creates (it may be changed elsewhere: the
# platform maintains fullscreen, for one). Adapters keep their own state to
# themselves, so none of it is listed.
STATE = {
    "ui/shell.py": (
        "root", "q", "ctl", "embedded", "fullscreen", "_swapped", "_was_fullscreen",
        "_closing", "_exit_hard", "stream_window", "_stream_choices", "_device_items",
        "_sync_state", "_synced_at", "_party_live", "_session_text", "_film_length", "_pos",
        "_settings", "_party", "_update_dialog",
        "player_var", "mute_var", "auto_var", "follow_var", "interval_var", "swap_var",
        "device_var", "stream_var", "hint_var", "check_updates_var",
    ),
    "ui/mainwin.py": (
        "file_lbl", "status_lbl", "sync_btn", "resync_btn", "offset_lbl", "timeline",
        "play_btn", "mute_btn", "subs_btn", "party_btn", "film_link",
    ),
    "ui/video.py": (
        "video_win", "video_frame", "video_menu", "_sub_menu", "_sub_pick", "fs_bar", "_wheel",
        "_osd_win", "_osd_after",
        "_fs_hook", "_fs_hwnd", "_fs_up", "_fs_after", "_fs_xy", "_fs_moved", "_fs_hold",
        "_fs_pos", "_fs_time", "_fs_track", "_fs_len", "_fs_play", "_fs_stop",
        "_fs_frame_back", "_fs_frame_fwd", "_fs_resync", "_fs_full", "_fs_status",
        "_fs_placed",
    ),
    "ui/update.py": (
        "_update_checking", "_update_offer", "_update_running", "_update_cancel",
        "_update_line",
    ),
    "ui/dialogs.py": ("SettingsDialog.win", "WatchPartyDialog.win", "UpdateDialog.win"),
}


# --- the check -----------------------------------------------------------

_REQUIRED = "<required>"
_EXPR = "<expression>"


def problems(ui_dir=None):
    """Check the ui/ tree against the contract by reading it with ast.

    Returns a list of readable problems; an empty list means it holds."""
    root = Path(ui_dir) if ui_dir else Path(__file__).resolve().parent
    out = []
    trees = {}
    for path in sorted(root.glob("*.py")):
        rel = f"ui/{path.name}"
        try:
            trees[rel] = ast.parse(path.read_text(encoding="utf-8"), str(path))
        except (OSError, SyntaxError, ValueError) as e:
            out.append(f"{rel}: cannot be read: {e}")
    modules = {rel: _defs(tree.body) for rel, tree in trees.items()}

    def find_class(rel, name):
        nodes = [n for n in modules.get(rel, {}).get(name, ()) if isinstance(n, ast.ClassDef)]
        if rel not in trees:
            out.append(f"{rel}: missing")
        elif len(nodes) != 1:
            out.append(f"{rel}: expected one class {name}, found {len(nodes)}")
        return nodes[0] if len(nodes) == 1 else None

    # Each of the App's classes, and the names each defines in its body.
    # The platform adapters are one slot: they override PlatformAdapter, and
    # only one of the two is ever in an App.
    slots = {}
    for rel, name in MIXINS.items():
        cls = find_class(rel, name)
        if cls:
            slots[f"{rel} {name}"] = [(rel, cls)]
    side = [(rel, cls) for rel, cls in ((r, find_class(r, n)) for r, n in ADAPTERS.items()) if cls]
    slots["the platform adapters"] = side
    where = {}                   # name -> labels of the slots defining it
    for label, members in slots.items():
        for rel, cls in members:
            for name, nodes in _defs(cls.body).items():
                if len(nodes) > 1:
                    out.append(f"{rel}: {cls.name}.{name} is defined {len(nodes)} times; the "
                               f"last one silently wins")
                where.setdefault(name, set()).add(label)
    for name, labels in sorted(where.items()):
        if len(labels) > 1:
            out.append(f"{name} is defined in {' and '.join(sorted(labels))}: the method "
                       f"order would pick one of them without a word")

    # every contract name, once, in its owner, with its signature
    for rel, entries in CONTRACT.items():
        mixin = MIXINS.get(rel)
        mixin_cls = slots.get(f"{rel} {mixin}", [(None, None)])[0][1] if mixin else None
        for name, entry in entries.items():
            sig = entry.split("; ", 1)[0].strip()
            if "." in name:
                cls_name, member = name.split(".", 1)
                cls = find_class(rel, cls_name)
                if cls is None:
                    continue
                nodes = _lookup(cls, member, modules[rel])
                if len(nodes) != 1:
                    out.append(f"{rel}: {name} is defined {len(nodes)} times, not once")
                    continue
                _check_entry(out, rel, name, sig, nodes[0], method=True)
                continue
            here = modules.get(rel, {}).get(name, [])
            mine = _defs(mixin_cls.body).get(name, []) if mixin_cls else []
            others = sorted(label for label in where.get(name, ())
                            if label != f"{rel} {mixin}")
            if not here and not mine:
                found = f"; it is in {' and '.join(others)} instead" if others else ""
                out.append(f"{rel}: {name} is missing{found}")
                continue
            count = len(here) + len(mine) + len(others)
            if count != 1:
                also = f" (also in {' and '.join(others)})" if others else ""
                out.append(f"{rel}: {name} is defined {count} times, not once{also}")
                continue
            _check_entry(out, rel, name, sig, (here or mine)[0], method=bool(mine))

    # the shared state, set by its owner
    for rel, names in STATE.items():
        for name in names:
            cls_name, attr = name.split(".", 1) if "." in name else (MIXINS.get(rel), name)
            cls = find_class(rel, cls_name) if cls_name else None
            if cls is None:
                continue
            classes = [cls] if "." not in name else _lineage(cls, modules[rel])
            if not any(attr in _self_attrs(c) for c in classes):
                out.append(f"{rel}: nothing in {cls_name} sets self.{attr}, which it owns")

    # both adapters fill in the contract
    base = find_class("ui/adapter.py", "PlatformAdapter")
    if base is not None:
        base_defs = _defs(base.body)
        required = [name for name, nodes in base_defs.items() if _required(nodes[0])]
        for rel in ("ui/platform_win.py", "ui/platform_mac.py"):
            cls = find_class(rel, "Adapter")
            if cls is None:
                continue
            if not any(ast.unparse(b).split(".")[-1] == "PlatformAdapter" for b in cls.bases):
                out.append(f"{rel}: Adapter does not subclass PlatformAdapter")
            defs = _defs(cls.body)
            missing = [name for name in required if name not in defs]
            if missing:
                out.append(f"{rel}: Adapter does not define {', '.join(missing)}")
            for name, nodes in defs.items():
                theirs = base_defs.get(name, [None])[0]
                if isinstance(theirs, ast.FunctionDef) and isinstance(nodes[0], ast.FunctionDef):
                    want, got = _shape(theirs, True), _shape(nodes[0], True)
                    if not _same(want, got):
                        out.append(f"{rel}: Adapter.{name}{_show(got)} does not match "
                                   f"PlatformAdapter.{name}{_show(want)}")

    # the queue's handlers exist; VIEWS lists the views
    shell = slots.get("ui/shell.py Shell", [(None, None)])[0][1]
    handlers = _literal(shell, "HANDLERS") if shell else None
    if shell and not isinstance(handlers, dict):
        out.append("ui/shell.py: Shell.HANDLERS is not a plain dict of strings")
    for kind, method in (handlers or {}).items():
        if method not in where:
            out.append(f"ui/shell.py: HANDLERS sends {kind!r} to {method}, which nothing "
                       f"defines")
    main = slots.get("ui/mainwin.py MainWindow", [(None, None)])[0][1]
    views = _literal(main, "VIEWS") if main else None
    if main:
        wanted = {n for n in CONTRACT["ui/mainwin.py"] if n not in ("VIEWS", "_build_main")}
        if not isinstance(views, (tuple, list)) or set(views) != wanted:
            out.append(f"ui/mainwin.py: MainWindow.VIEWS must list exactly "
                       f"{', '.join(sorted(wanted))}")
    return list(dict.fromkeys(out))              # a missing module is said once


def _defs(body):
    """name -> [node] for the functions, classes and assigned names defined
    directly in `body` (a property's setter is the same name, not another)."""
    found = {}
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(isinstance(d, ast.Attribute) and d.attr in ("setter", "deleter")
                   for d in node.decorator_list):
                continue
            found.setdefault(node.name, []).append(node)
        elif isinstance(node, ast.ClassDef):
            found.setdefault(node.name, []).append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                for name in _flat(target):
                    if isinstance(name, ast.Name):
                        found.setdefault(name.id, []).append(node)
    return found


def _lineage(cls, module):
    """cls, then the bases it has in its own module, nearest first (a
    dialog's shared base class, say)."""
    found, queue = [], [cls]
    while queue:
        c = queue.pop(0)
        if c in found:
            continue
        found.append(c)
        for base in c.bases:
            name = ast.unparse(base).split(".")[-1]
            queue += [n for n in module.get(name, ()) if isinstance(n, ast.ClassDef)]
    return found


def _lookup(cls, member, module):
    """The definitions of `member` that cls's instances get: from the first
    class in its lineage that defines it."""
    for c in _lineage(cls, module):
        nodes = _defs(c.body).get(member, [])
        if nodes:
            return nodes
    return []


def _flat(target):
    if isinstance(target, (ast.Tuple, ast.List)):
        return [n for el in target.elts for n in _flat(el)]
    if isinstance(target, ast.Starred):
        return _flat(target.value)
    return [target]


def _self_attrs(cls):
    """Every attribute some method of `cls` sets on self."""
    found = set()
    for node in ast.walk(cls):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            continue
        for target in targets:
            for el in _flat(target):
                if (isinstance(el, ast.Attribute) and isinstance(el.value, ast.Name)
                        and el.value.id == "self"):
                    found.add(el.attr)
    return found


def _decorators(fn):
    return {ast.unparse(d) for d in fn.decorator_list}


def _check_entry(out, rel, name, sig, node, method):
    if sig.startswith("("):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or (
                "property" in _decorators(node)):
            out.append(f"{rel}: {name} should be a function or method")
            return
        want = _shape(ast.parse(f"def _({_inside(sig)}): pass").body[0], False)
        got = _shape(node, method)
        if not _same(want, got):
            out.append(f"{rel}: {name}{_show(got)} does not match the contract's {name}"
                       f"{_show(want)}")
    elif sig.startswith("property"):
        if not isinstance(node, ast.FunctionDef) or "property" not in _decorators(node):
            out.append(f"{rel}: {name} should be a property")
    elif sig.startswith("class attribute"):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            out.append(f"{rel}: {name} should be a value in the class body")
    elif sig.startswith("class"):
        if not isinstance(node, ast.ClassDef):
            out.append(f"{rel}: {name} should be a class")


def _inside(sig):
    """The text between a signature's outer brackets."""
    depth = 0
    for i, ch in enumerate(sig):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return sig[1:i]
    raise ValueError(f"unbalanced signature: {sig}")


def _shape(fn, method):
    """A function's parameters: ([(name, default)], [(kw-only name, default)],
    *args?, **kw?, staticmethod?), without self for a method."""
    args = fn.args
    pos = list(args.posonlyargs) + list(args.args)
    defaults = [None] * (len(pos) - len(args.defaults)) + list(args.defaults)
    pairs = [(p.arg, _default(d)) for p, d in zip(pos, defaults)]
    static = "staticmethod" in _decorators(fn)
    if method and not static and pairs:
        pairs = pairs[1:]
    kw = [(p.arg, _default(d)) for p, d in zip(args.kwonlyargs, args.kw_defaults)]
    return pairs, kw, args.vararg is not None, args.kwarg is not None, static


def _default(node):
    if node is None:
        return _REQUIRED
    try:
        return repr(ast.literal_eval(node))
    except Exception:                            # not a literal: time.monotonic, say
        return _EXPR


def _same(want, got):
    """got matches want: the same names in the same order, the same
    parameters optional, and the same literal defaults where want has one."""
    if want[2:] != got[2:]:                      # *args, **kw, staticmethod
        return False
    for mine, theirs in ((want[0], got[0]), (want[1], got[1])):
        if [n for n, _ in mine] != [n for n, _ in theirs]:
            return False
        for (_, d1), (_, d2) in zip(mine, theirs):
            if (d1 == _REQUIRED) != (d2 == _REQUIRED):
                return False
            if d1 not in (_REQUIRED, _EXPR) and d1 != d2:
                return False
    return True


def _show(shape):
    pairs, kw, star, starstar, static = shape
    parts = [n if d == _REQUIRED else f"{n}={d}" for n, d in pairs]
    if star or kw:
        parts.append("*args" if star else "*")
    parts += [n if d == _REQUIRED else f"{n}={d}" for n, d in kw]
    if starstar:
        parts.append("**kw")
    return f"({', '.join(parts)})" + (" as a staticmethod" if static else "")


def _required(node):
    """A PlatformAdapter member every adapter must define: a method whose
    whole body raises NotImplementedError, or a constant left empty."""
    if isinstance(node, ast.FunctionDef):
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]                      # the docstring
        if len(body) == 1 and isinstance(body[0], ast.Raise) and body[0].exc is not None:
            exc = body[0].exc.func if isinstance(body[0].exc, ast.Call) else body[0].exc
            return isinstance(exc, ast.Name) and exc.id == "NotImplementedError"
        return False
    return (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
            and node.value.value == "")


def _literal(cls, name):
    for node in cls.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            try:
                return ast.literal_eval(node.value)
            except Exception:
                return None
    return None


if __name__ == "__main__":
    found = problems()
    for line in found:
        print(line)
    names = sum(len(entries) for entries in CONTRACT.values())
    print(f"ui contract: {'OK' if not found else f'{len(found)} problem(s)'} "
          f"({names} names, {sum(len(v) for v in STATE.values())} shared attributes)")
    raise SystemExit(1 if found else 0)
