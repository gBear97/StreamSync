"""StreamSync - sync your own copy of a film to a live stream.

It records a few seconds of the stream's sound (WASAPI loopback on
Windows, a Core Audio tap of this Mac's sound on macOS) and finds the
matching moment in the film's own audio track, so it works with the
stream minimized.

Playback goes to the built-in player (libvlc drawing into our own window,
millisecond seeks) or to the real VLC app driven over its HTTP interface.
"Keep in sync automatically" re-checks in the background, corrects drift,
and can follow the streamer's pauses.

This module is the window's logic both platforms share (Shell), the App
put together from the ui mixins and the platform's adapter, and main().
`python -m ui.shell` runs it without streamsync.py's dependency gate.
"""

import os
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox

import webbrowser

import audio_capture
import controller
import diagnostics
import hosting
import matcher
from players import VLCError
from ui import adapter, dialogs, mainwin, theme, update, video, widgets  # noqa: F401


class Shell(adapter.PlatformAdapter):
    """The logic both platforms share. PlatformAdapter's stubs sit last in
    the App's method order, behind the platform's real ones."""

    HANDLERS = {   # kind -> method name, looked up on the instance at dispatch time
        "status": "_set_status", "session": "_on_session_text", "swap": "_stream_swap",
        "swapdone": "_swap_done", "devices": "_apply_devices", "apps": "_apply_stream_list",
        "mics": "_on_mics", "film_info": "_on_film_info", "busy_off": "_on_busy_off",
        "auto_off": "_on_auto_off", "synced": "_on_synced",
        "update": "_on_update", "update_none": "_on_update_none", "update_err": "_on_update_err",
        "update_progress": "_on_update_progress", "update_step": "_on_update_step",
        "update_installing": "_on_update_installing", "update_ready": "_on_update_ready",
        "update_failed": "_on_update_failed", "update_cancelled": "_on_update_cancelled",
        "hosting": "_on_hosting", "hosting_ready": "_on_hosting_ready",
        "hosting_failed": "_on_hosting_failed"}

    TEXT_CLASSES = {"Entry", "TEntry", "Text", "Spinbox", "TSpinbox", "TCombobox"}

    def __init__(self, root):
        # shared state first, then the platform's and the update flow's (no Tk)
        self.root = root
        self.q = queue.Queue()
        self.fullscreen = False
        self._swapped = False        # stream window currently shown?
        self._was_fullscreen = False  # we owe the user fullscreen back
        self._closing = False
        self._exit_hard = False
        self.stream_window = ""      # the stream's window or app; "" is Automatic
        self._stream_choices = []
        self._device_items = []
        self._sync_state = "none"
        self._synced_at = None       # monotonic time of the last sync that moved the film
        self._party_live = False
        self._session_text = ""
        self._party_code = ""        # Join's session code, kept while Watch party is closed
        self._hosting_params = None  # ctl.host's arguments while the tunnel opens
        self._film_length = None
        self._pos = (None, None)     # the time and length the last tick read
        self._settings = self._party = self._update_dialog = None
        # what the tick last showed, so it only redraws on a change
        self._ago_shown = None
        self._link_shown = None
        self._sub_shown = None
        self._subs_popup = None
        self._init_platform()
        self._init_updates()

        root.title("StreamSync")
        root.resizable(False, False)
        self._init_theme()
        root.configure(bg=theme.GROUND)
        self._init_root()
        self._make_vars()
        self._build_video_window()
        self._build_video_menu()
        self._build_fs_bar()         # before any _set_status: it mirrors into the bar
        self._build_main()
        self._bind_keys(root)
        self._style_window(root)     # the dialogs' dark title bar, here too

        try:
            self.embedded = self._make_player()
        except VLCError as e:
            if _selftest():
                # Nobody can dismiss a modal in an unattended run, and a
                # blocked dialog looks identical to a hung app.
                print(f"SELFTEST: VLC unavailable - {e}", file=sys.stderr)
                raise SystemExit(2)
            # The main window is not up yet, so this dialog is the entire
            # user interface in this failure and has to carry the
            # diagnosis itself.
            diagnostics.log(f"player failed to start: {e}")
            messagebox.showerror("StreamSync - VLC problem", str(e))
            raise SystemExit(1)
        self.ctl = controller.SyncController(self.q, self.embedded)

        self._load_config()
        self._populate_audio_devices()
        self.ctl.start()
        self._start_platform()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(80, self._poll_queue)
        root.after(700, self._tick_time)
        if self.check_updates_var.get() and not _selftest():
            # Quiet: nobody asked, so it only speaks up when there is
            # something newer. Late-bound: a test may replace _check_updates
            # on the instance.
            root.after(2500, lambda: self._check_updates(quiet=True))

    def _init_theme(self):
        theme.init(self._ui_scale(), self._ui_font_family())

    def _make_vars(self):
        # every one before any widget refers to it; the film menu shares mute_var
        r = self.root
        self.player_var = tk.StringVar(r, value="embedded")
        self.mute_var = tk.BooleanVar(r, value=True)
        self.auto_var = tk.BooleanVar(r, value=False)
        self.follow_var = tk.BooleanVar(r, value=True)
        self.interval_var = tk.IntVar(r, value=30)
        self.swap_var = tk.BooleanVar(r, value=True)
        self.device_var = tk.StringVar(r, value="")
        self.stream_var = tk.StringVar(r, value="")
        self.hint_var = tk.StringVar(r, value="")
        self.check_updates_var = tk.BooleanVar(r, value=True)

    # --- players and the film ---------------------------------------------

    @property
    def player(self):
        return self.ctl.player

    @property
    def external(self):
        return self.ctl.external

    def _choose_file(self):
        path = filedialog.askopenfilename(
            title="Choose the local copy of the film",
            filetypes=[("Video files", "*.mkv *.mp4 *.avi *.mov *.m4v *.ts *.webm *.wmv"),
                       ("All files", "*.*")])
        if not path:
            return
        if self.player is self.embedded:
            # Shown (and, on the Mac, handed to libvlc) before the load:
            # a film loaded into a window that did not exist yet played
            # with nothing to look at.
            self._show_video_window()
        try:
            self.ctl.load_file(path)
        except VLCError as e:
            if not self.ctl.video_path:
                self.video_win.withdraw()    # nothing in it: FirstRun stays as it was
            messagebox.showerror("StreamSync", str(e))
            return
        self._film_loaded(path)
        if self.player is self.embedded:
            # Space plays it too, sync or not: the line used to say that
            # playback waited for the first sync
            self._set_status(f"Loaded {Path(path).name}. Sync to stream starts it where "
                             "the stream is; Space plays it from here.")
        else:
            self._set_status("Opening in external VLC...")
        self._save_config()

    def _film_loaded(self, path):
        self._film_length = None
        self._synced_at = None
        self._sub_shown = None       # another film: its subtitles are read afresh
        self._show_film(Path(path).name, None)
        self._set_sync_state("none")
        self._show_main_state()
        if self._party is not None:
            # it stays open beside the main window now: Host and Join
            # wait for a film, and this is the film
            self._party.refresh()

        # The length is known before playback starts, so the timeline and
        # the header can show it at once; ffmpeg reads it in a moment.
        def probe():
            try:
                seconds = matcher.probe(path)[0]
            except Exception:
                seconds = None       # the player's own length takes over once it plays
            self.q.put(("film_info", path, seconds))
        threading.Thread(target=probe, daemon=True).start()

    def _on_film_info(self, path, seconds):
        if path != self.ctl.video_path:
            return                   # another film was opened while this one was read
        self._film_length = seconds
        self._show_film(Path(path).name, seconds)

    def _apply_player_choice(self):
        if self.player_var.get() == "external":
            err = self.ctl.use_external(self.mute_var.get())
            if err:
                messagebox.showerror("StreamSync", err)
                self.player_var.set("embedded")
                return
            if self.fullscreen:
                # leave first: a film window gone fullscreen and then
                # hidden (or left up, on the Mac) would come back covering
                # the screen with nothing playing in it
                self._set_fullscreen(False, show=False)
            if self.WITHDRAW_FILM_FOR_EXTERNAL:
                self.video_win.withdraw()
        else:
            try:
                self.ctl.use_embedded()
            except VLCError as e:
                self._set_status(f"The built-in player could not open the film: {e}")
            else:
                if self.ctl.video_path:
                    # The show-and-attach every other way into the built-in
                    # player does: a film opened while External VLC was in use
                    # never got a window, and one closed meanwhile stays shut.
                    self._show_video_window()
        if self.ctl.video_path:
            self._show_film(Path(self.ctl.video_path).name, self._film_length)  # the player's name
        self._on_mute_toggle()
        self._save_config()

    # --- syncing -----------------------------------------------------------

    def _ready(self):
        if not self.ctl.video_path:
            messagebox.showinfo("StreamSync", "Open a film first.")
            return False
        return True

    def _sync(self, resync=False, whole=False):
        """Start a sync; True if one started (False: one was already
        running, or the setup is not ready)."""
        if self.ctl.busy:
            return False
        if not self._ready():
            return False
        run = self.ctl.resync if resync else self.ctl.sync
        hint = "" if whole else self.hint_var.get()   # blank: the whole film
        try:
            started = run(hint, mute=self.mute_var.get())
        except ValueError as e:
            messagebox.showerror("StreamSync", str(e))
            return False
        if started:
            self._set_busy(True)
        return bool(started)

    def _resync(self):
        return self._sync(resync=True)

    def _set_busy(self, on):
        # the fullscreen bar's too, whichever button started it (PR 31's
        # review: a second sync from the bar used to start under the first)
        state = "disabled" if on else "normal"
        for b in (self.sync_btn, self.resync_btn, self._fs_resync, self._fs_full):
            b.config(state=state)
        if on:
            self._sync_state = "syncing"
            self._show_sync_state("syncing")

    def _on_busy_off(self):
        self._set_busy(False)
        self._fs_sync_done()
        if self._sync_state == "syncing":
            # finished with no result: the worker said why in the status
            self._set_sync_state("failed")

    def _on_synced(self, info):
        """A sync, an auto check or a resume decided on a match."""
        # where the stream is now: the match was heard at perf_counter `at`
        at = info["match"] + max(0.0, time.perf_counter() - info["at"])
        if info["trusted"]:
            state = "synced"
        elif info["applied"]:
            state = "weak"           # a first guess: the film had no position to keep
        else:
            state = "missed"         # the film was left where it was
        self._synced_at = time.monotonic() if state != "missed" else None
        headline = self._set_sync_state(state, at)
        if info["trusted"] and info["applied"] and info["source"] == "sync":
            # the state row says it all now; the score stays in the log
            self._set_status("")
        self._fs_status.config(text=headline)

    def _set_sync_state(self, state, at=None):
        """The sync-state row, and the 'ago' that goes with it; returns its
        headline."""
        self._sync_state = state
        headline = self._show_sync_state(state, at)
        self._ago_shown = None       # recomputed now, not at the next tick
        self._tick_ago()
        return headline

    def _on_auto_toggle(self):
        try:
            interval = self.interval_var.get()
        except (ValueError, tk.TclError):
            interval = 30
        self.ctl.set_auto(self.auto_var.get(), self.follow_var.get(), interval)
        self._save_config()
        self._show_auto_sub()

    def _on_auto_off(self, text):
        # auto mode gave up (details in the log): untick it
        # exactly as a manual uncheck would, then say why
        self.auto_var.set(False)
        self._on_auto_toggle()
        self._set_status(text)

    # --- playback ------------------------------------------------------------

    def _nudge(self, delta):
        offset = self.ctl.nudge(delta)
        self._show_offset(offset)

    def _reset_offset(self):
        self.ctl.reset_offset()
        self._show_offset(0.0)

    def _toggle_pause(self):
        try:
            was_playing = self.player.is_playing()
        except Exception:
            was_playing = False
        self.player.toggle_pause()
        # pausing brings the streamer's window up; resuming brings the film back
        self._stream_swap(was_playing, "Space or a play button")

    def _toggle_mute(self):
        self.mute_var.set(not self.mute_var.get())
        self._on_mute_toggle()

    def _on_mute_toggle(self):
        self.ctl.set_mute(self.mute_var.get())
        self._show_mute()

    def _toggle_fullscreen(self):
        if not self.ctl.video_path:
            # nothing to show: the film window would cover the screen
            # empty, and the VLC app's toggle is an HTTP call to nothing
            return
        self._fullscreen_from_window()
        if self.player is self.embedded:
            self._set_fullscreen(not self.fullscreen)
        else:
            self.external.fullscreen_toggle()

    def _leave_fullscreen_key(self):
        self._fullscreen_from_window()
        if self.fullscreen and self.player is self.embedded:
            # show=False: Esc must not bring back a film window someone hid
            self._set_fullscreen(False, show=False)

    def _seek_by(self, delta):
        t = self.player.time()
        if t is None:
            return                   # stopped or never started: nothing to seek from
        n = self.player.length() or self._film_length
        target = t + delta
        if n:
            target = min(target, n - 1)   # a film run off its end stops
        self.player.seek(max(0.0, target))
        self._mark_moved()

    def _can_seek(self):
        t, n = self._pos
        return t is not None and bool(n)

    def _seek_to_frac(self, frac):
        n = self._pos[1]
        if not n:
            return
        self.player.seek(frac * n)
        self._mark_moved()

    def _mark_moved(self):
        if self._sync_state in ("synced", "weak"):
            self._fs_status.config(text=self._set_sync_state("moved"))

    # --- keys -----------------------------------------------------------------

    def _bind_keys(self, toplevel):
        for sequence, action in (("<space>", "pause"), ("<Left>", "back"),
                                 ("<Right>", "forward"), ("<KeyPress-f>", "fullscreen"),
                                 ("<KeyPress-F>", "fullscreen"), ("<Escape>", "leave")):
            toplevel.bind(sequence, lambda e, a=action: self._on_key(a, e))

    def _on_key(self, action, event):
        if self._typing(event):
            return None              # the field keeps its key
        if not self.ctl.video_path:
            # FirstRun: nothing to play, seek or fullscreen (F would cover
            # the screen with an empty film window; on the Mac it set
            # fullscreen on an unattached libvlc and left self.fullscreen True)
            return "break"
        {"pause": self._toggle_pause, "back": lambda: self._seek_by(-5),
         "forward": lambda: self._seek_by(+5), "fullscreen": self._toggle_fullscreen,
         "leave": self._leave_fullscreen_key}[action]()
        return "break"

    def _typing(self, event):
        try:
            return event.widget.winfo_class() in self.TEXT_CLASSES
        except Exception:            # a widget path tkinter cannot resolve, a destroyed widget
            return False

    # --- dialogs and the watch party -------------------------------------------

    def _open_settings(self):
        if self._settings is not None:
            self._lift_dialog(self._settings.win)
            return
        dialogs.SettingsDialog(self)    # it registers itself as _settings

    def _open_watch_party(self):
        if self._party is not None:
            self._lift_dialog(self._party.win)
            return
        dialogs.WatchPartyDialog(self)  # it registers itself as _party

    def _lift_dialog(self, win):
        win.deiconify()
        win.lift()
        try:
            win.focus_force()
        except Exception:
            pass                     # the keyboard is a nicety; the window is up

    def _session_running(self):
        return self.ctl.session_running()

    def _session_changed(self):
        self._party_live = self.ctl.session_running()
        self._show_party_live(self._party_live)
        if self._party is not None:
            self._party.refresh()

    def _on_session_text(self, text):
        self._session_text = text
        self._set_status(text)
        if self._party is not None:
            self._party.set_session_text(text)

    def _leave_session(self):
        self.ctl.leave()
        self._session_changed()
        self._set_status("Left the session.")

    # --- hosting through this computer ----------------------------------------

    def _host_here(self, params, exe, download):
        """Host through this computer: the relay and a Cloudflare quick
        tunnel open on a worker thread (cloudflared downloaded first if
        `download`), and the session starts from the queue once the
        tunnel's address is live. params: ctl.host's arguments after
        relay_url."""
        self._hosting_params = params
        q, ctl = self.q, self.ctl
        h = ctl.begin_hosting()          # here, so Cancel finds it at once

        def work():
            try:
                local, public = ctl.open_tunnel(h, exe, download,
                                                lambda text: q.put(("hosting", text)))
            except hosting.Cancelled:
                return
            except Exception as e:
                q.put(("hosting_failed", str(e) or type(e).__name__,
                       isinstance(e, hosting.DownloadError)))
                return
            q.put(("hosting_ready", local, public))
        threading.Thread(target=work, daemon=True).start()

    def _hosting_now(self):
        """True while this computer's tunnel is opening, before the session."""
        return self.ctl.hosting is not None and self.ctl.session is None

    def _on_hosting(self, text):
        if not self._hosting_now():
            return                   # cancelled: a step reported late
        self._set_status(text)
        if self._party is not None:
            self._party.set_host_note(text)

    def _on_hosting_ready(self, local, public):
        h = self.ctl.hosting
        if self._hosting_params is None or h is None or h.public_url != public:
            # cancelled while it opened, or an older start's tunnel: the
            # start that is still opening keeps its params
            return
        params, self._hosting_params = self._hosting_params, None
        try:
            self.ctl.host(local, remember_relay=False, **params)
        except Exception as e:
            self.ctl.stop_hosting()
            self._on_hosting_failed(f"could not start ({e})", False)
            return
        self.ctl.public_relay = public
        self._session_changed()
        self._save_config()
        self._set_status("Hosting through a free Cloudflare tunnel - Copy invite gives "
                         "your viewers the address and code.")

    def _on_hosting_failed(self, reason, manual):
        self._hosting_params = None
        self._set_status(f"Could not host through this computer: {reason}")
        if self._party is not None:
            self._party.hosting_failed(reason, manual)

    def _cancel_hosting(self):
        self._hosting_params = None
        self.ctl.stop_hosting()
        self._set_status("Hosting cancelled.")
        if self._party is not None:
            self._party.refresh()

    def _open_cloudflared_page(self):
        webbrowser.open(hosting.DOWNLOAD_PAGE)

    # --- devices and the stream list ------------------------------------------

    def _populate_audio_devices(self):
        def work():
            try:
                names = audio_capture.list_speakers()
                default = audio_capture.default_speaker_name()
            except Exception as e:
                self.q.put(("status", f"Could not list audio devices: {e}"))
                return
            self.q.put(("devices", [f"(default) {default}"] + names))
        threading.Thread(target=work, daemon=True).start()

    def _apply_devices(self, items):
        self._device_items = list(items)
        if not self.device_var.get():
            if self.ctl.audio_device:
                for n in items[1:]:
                    if self.ctl.audio_device.lower() in n.lower():
                        self.device_var.set(n)
                        break
            if not self.device_var.get():
                self.device_var.set(items[0])
        if self._settings is not None:
            self._settings.set_devices(items)
        self._show_listen(self._device_label(self.device_var.get()))

    def _on_device_pick(self, _event=None):
        val = self.device_var.get()
        self.ctl.audio_device = "" if val.startswith("(default)") else val
        self._save_config()
        self._show_listen(self._device_label(val))

    def _device_label(self, value):
        if value.startswith("(default)"):
            name = value[len("(default)"):].strip()
            return f"{name} ({self.DEFAULT_DEVICE_NOTE})" if name else self.DEFAULT_DEVICE_NOTE
        return value

    def _populate_mics(self):
        def work():
            try:
                names = audio_capture.list_microphones()
            except Exception:
                names = []           # the dialog still offers "Default microphone"
            self.q.put(("mics", names))
        threading.Thread(target=work, daemon=True).start()

    def _on_mics(self, names):
        if self._party is not None:
            self._party.set_mics(names)

    def _apply_stream_list(self, items):
        self._stream_choices = list(items)
        if self._settings is not None:
            self._settings.set_stream_choices(items)

    # --- subtitles ---------------------------------------------------------------

    def _open_subs_menu(self):
        if self.player is not self.embedded:
            self._set_status("Subtitle picker applies to the embedded player; "
                             "in external mode use VLC's Subtitle menu.")
            return
        if self._subs_popup is None:
            self._subs_popup = tk.Menu(self.root, tearoff=0)
        m = self._subs_popup
        self._fill_sub_menu(m)       # the tracks as they are now, the current one ticked
        b = self.subs_btn
        try:
            m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())
        finally:
            m.grab_release()

    def _load_sub_file(self):
        if self.player is not self.embedded:
            self._set_status("Load subtitle files through VLC's own menu in "
                             "external mode.")
            return
        path = filedialog.askopenfilename(
            title="Choose a subtitle file",
            filetypes=[("Subtitles", "*.srt *.ass *.ssa *.sub *.vtt"),
                       ("All files", "*.*")])
        if path:
            self.embedded.add_subtitle_file(path)
            # libvlc lists the new track a moment later
            self.root.after(600, self._tick_subtitle)

    # --- plumbing ------------------------------------------------------------------

    def _set_status(self, text):
        self.status_lbl.config(text=text)
        self._fs_status.config(text=text)    # readable in fullscreen too
        self._show_status_line(bool(text))

    def _poll_queue(self):
        try:
            while not self._closing:
                try:
                    kind, *payload = self.q.get_nowait()
                except queue.Empty:
                    break
                name = self.HANDLERS.get(kind)
                # a kind with no handler is ignored; an exception goes to
                # Tk's hook, which logs it
                if name:
                    getattr(self, name)(*payload)
            if not self._closing:
                self._settle_fullscreen()      # Mac: the fullscreen check; Windows: nothing
        finally:
            # One bad event never stops the loop. A handler that closed the
            # app (an update's restart) ends it: after root.destroy() the
            # windows are gone - `wm` and every widget command raise - so
            # nothing more is dispatched, settled or scheduled.
            if not self._closing:
                self.root.after(80, self._poll_queue)

    def _tick_time(self):
        try:
            p = self.player
            t, n, playing = p.time(), p.length(), p.is_playing()
            n = n or self._film_length     # the player knows it only once playing
            self._pos = (t, n)
            self._show_position(t, n, playing)
        except Exception:
            pass
        try:
            self._tick_status()
        except Exception:
            pass
        if not self._closing:
            self.root.after(700, self._tick_time)

    def _tick_status(self):
        self._tick_ago()
        # a session that ended by itself (a host gone, the relay down)
        live = self.ctl.session_running()
        if live != self._party_live:
            self._session_changed()
        if live and self._party is not None:
            self._party.refresh_code()     # a host's code comes from the relay, seconds in
        link = (bool(self.ctl.video_path) and self.player is self.embedded
                and self.video_win.state() == "withdrawn")
        if link != self._link_shown:
            self._link_shown = link
            self._show_film_link(link)
        self._tick_subtitle()

    def _tick_ago(self):
        if self._sync_state in ("synced", "weak") and self._synced_at is not None:
            s = int(time.monotonic() - self._synced_at)
            ago = f"{s} s ago" if s < 60 else f"{s // 60} min ago"
        else:
            ago = ""
        if ago != self._ago_shown:
            self._ago_shown = ago
            self._show_ago(ago)

    def _tick_subtitle(self):
        if self.player is not self.embedded:
            if self._sub_shown != "vlc":
                self._sub_shown = "vlc"
                self._show_subtitle("in VLC")
            return
        cur = self.embedded.current_subtitle()
        if cur == self._sub_shown:
            return                   # the track list is only read on a change
        if cur == -1:
            text = "Off"
        else:
            name = dict(self.embedded.subtitle_tracks()).get(cur)
            if name is None:
                return               # not listed yet: libvlc lists tracks once playing
            text = "Off" if name == "Disable" else name
        self._sub_shown = cur
        self._show_subtitle(text)

    def _save_config(self):
        self.ctl.save_config({"hint": self.hint_var.get(), "player": self.player_var.get(),
                              "swap": self.swap_var.get(), "stream_window": self.stream_window,
                              "check_updates": bool(self.check_updates_var.get())})

    def _load_config(self):
        cfg = self.ctl.load_config()
        path = self.ctl.film_to_restore(cfg)
        if path:
            # The same show-then-load as _choose_file: this is the path a
            # second launch takes, and it was the one that played films
            # into a window that did not exist.
            self._show_video_window()
            try:
                self.ctl.load_file(path)          # on failure ctl.video_path stays None
            except VLCError as e:
                self.video_win.withdraw()         # no film in it: FirstRun shows, with the reason
                self._set_status(f"Could not reopen {Path(path).name}: {e}")
            else:
                self._film_loaded(path)
        if cfg.get("hint"):
            self.hint_var.set(cfg["hint"])
        self.interval_var.set(self.ctl.auto_interval)
        self.follow_var.set(self.ctl.auto_follow)
        self.swap_var.set(bool(cfg.get("swap", True)))
        self.check_updates_var.set(bool(cfg.get("check_updates", True)))
        # 1.x saved the pick under its platform's own key; read it once -
        # the next save writes stream_window only
        self.stream_window = cfg.get("stream_window", cfg.get(self.OLD_STREAM_KEY, "")) or ""
        if self.stream_window:
            self.stream_var.set(self.stream_window)
        self._show_auto_sub()
        # note: player choice is restored as "embedded"; external VLC is only
        # spawned when the user picks it, never on startup

    def _on_close(self):
        if self._closing:
            return                    # the close box, Command-Q and an update can each arrive
        self._closing = True
        self._close_platform()        # the Mac's swap worker stops before the controller
        self._cancel_update()
        self._save_config()
        self.ctl.close()
        self._exit_hard = True        # saved and stopped: main() ends the process at once
        self.root.destroy()

    def _selftest_tour(self):
        """Open and close every dialog once, the way a user reaches it:
        Settings, Watch party, and the offer the startup check would make
        for a version that does not exist. Nothing is downloaded - only
        the offer's Install button starts that."""
        # The selftest's close is on a timer, and the Mac's dialogs pump
        # events as they show: on a slow machine the close can come in the
        # middle, and the steps after it would only fail against a
        # destroyed window.
        steps = (self._open_settings, lambda: self._settings.close(),
                 self._open_watch_party, lambda: self._party.close(),
                 lambda: self._on_update("v9.9.9", {"tag_name": "v9.9.9", "assets": []}),
                 lambda: self._update_dialog.close())
        for step in steps:
            if self._closing:
                return
            step()


def app_class(platform):
    """The App for one platform: (Adapter, FilmWindow, MainWindow, UpdateFlow, Shell).
    MRO = App, Adapter, FilmWindow, MainWindow, UpdateFlow, Shell, PlatformAdapter, object:
    the adapter's seams and constants win over the shared ones."""
    return type("App", (platform.Adapter, video.FilmWindow, mainwin.MainWindow,
                        update.UpdateFlow, Shell), {"__module__": __name__})


def default_platform():
    # static import statements: PyInstaller's modulegraph finds both
    if sys.platform == "darwin":
        from ui import platform_mac
        return platform_mac
    from ui import platform_win
    return platform_win


def create_app(root, platform=None):
    """Build the app on `root`. Tests pass the platform module: test_macswap
    runs platform_mac on every OS."""
    return app_class(platform or default_platform())(root)


def _selftest():
    return "--selftest" in sys.argv


def _selftest_config():
    """An unattended run must never read the user's film or write their picks."""
    folder = Path(tempfile.mkdtemp(prefix="streamsync-selftest-"))
    controller.CONFIG_PATH = folder / "streamsync.json"


def main():
    platform = default_platform()
    # Windows: per-monitor DPI awareness, before any Tk window
    platform.Adapter.prepare_process()
    if _selftest():
        # before create_app: _load_config must not see the user's file
        _selftest_config()
    root = tk.Tk()
    # A windowed build has no console: without these, an exception on a
    # worker thread or in a Tk callback leaves no trace anywhere.
    diagnostics.install_excepthook()
    diagnostics.install_tk_hook(root)
    errors = _selftest_errors(root) if _selftest() else []
    app = create_app(root, platform)
    app._present_root()                        # the main window on top, with the keyboard
    if _selftest():
        # open and close Settings, Watch party and the Update offer, then
        # the real close path, which saves only to the temporary config
        root.after(600, app._selftest_tour)
        root.after(3000, app._on_close)
    root.mainloop()
    if _selftest() or app._exit_hard:
        code = 0
        if _selftest():
            if errors:
                print(f"SELFTEST FAILED - {len(errors)} error(s) in Tk callbacks (details in "
                      f"the log), the first: {errors[0]}", file=sys.stderr)
                code = 1
            else:
                # print() with no stdout (a windowed build) does nothing
                print("SELFTEST OK")
        # libvlc leaves native threads running that a plain return would
        # wait on; a check, a quit or an update's restart must never hang.
        # _on_close has saved the settings and stopped the engine by now.
        for stream in (sys.stdout, sys.stderr):
            try:
                if stream:
                    stream.flush()
            except Exception:
                pass
        os._exit(code)


def _selftest_errors(root):
    """The selftest's list of exceptions raised in Tk callbacks - a dialog
    on the tour, a queue handler, a tick. Tk's hook only logs them, so
    without this the run would say OK whatever broke."""
    errors = []
    logged = root.report_callback_exception

    def report(exc_type, exc, tb):
        errors.append(f"{exc_type.__name__}: {exc}")
        logged(exc_type, exc, tb)
    root.report_callback_exception = report
    return errors


if __name__ == "__main__":                     # `python -m ui.shell` skips the dependency gate
    main()
