"""The macOS adapter: what the shared shell does differently on a Mac.

- The picture: libvlc is handed the film window's NSView the first time
  the window shows (players.attach_tk), not at construction, because the
  view is only real once the window is on screen.
- Fullscreen is the film window's own, and macOS can drop a request made
  while the window is still animating, so every request is checked and
  made again until it has taken (_settle_fullscreen, on every _poll_queue
  pass). The green button changes the window behind the app's back, so a
  user's F or Esc reads the window first (_fullscreen_from_window).
- The stream-pause swap brings the stream's browser forward and hides it
  again through System Events (macwindowctl, osascript). Each round trip
  can block for seconds, so they run on a worker thread (_swap_worker),
  and the result comes back through the queue as "swapdone".
- The app menu: About, Settings (Command-,) and Quit (Command-Q), which
  goes through the app's own close.
- An update swaps the installed .app for the new one and relaunches.

macwindowctl only runs osascript in a subprocess, and AppKit is never
imported, so this module imports on every OS: test_macswap drives it over
a fake Tk on all three.
"""

import os
import queue
import re
import shutil
import subprocess
import threading
import time

import tkinter as tk

import depcheck
import diagnostics
import macwindowctl
import updater
from players import EmbeddedPlayer
from ui import adapter

BROWSERS = ("Safari", "Google Chrome", "Firefox", "Arc", "Brave Browser",
            "Microsoft Edge", "Opera", "Vivaldi")

# macOS takes a moment to move a window into or out of fullscreen, and a
# request made during that moment is dropped without a word: measured on
# macOS 27 with Tk 8.6.16, one made up to 0.5 s after the last change was
# lost and one made 0.8 s after it was taken. So a request is checked, and
# made again every FULLSCREEN_RETRY seconds until it has taken - for
# FULLSCREEN_SETTLE seconds, after which the screen is believed.
FULLSCREEN_RETRY = 0.25
FULLSCREEN_SETTLE = 3.0


class Adapter(adapter.PlatformAdapter):
    NAME = "mac"
    # Step 5 turns the film window's mouse controls and the bar on, once
    # they have been checked by hand on a Mac.
    FILM_CONTROLS = False
    FOCUS_FILM_ON_FULLSCREEN = True
    WITHDRAW_FILM_FOR_EXTERNAL = False
    MENU_BUTTONS = ("<Button-3>", "<Button-2>", "<Control-Button-1>")
    SIDEWAYS_WHEEL = ("<Shift-MouseWheel>",)
    WHEEL_UNIT = 60
    WHEEL_QUIET = 0.25
    FILM_GEOMETRY = (960, 540, None)
    OLD_STREAM_KEY = "stream_app"
    STREAM_LABEL = "Stream app"
    STREAM_AUTO_LABEL = "Automatic (first browser found)"
    REFRESH_STREAMS_ON_SETTINGS = False
    LISTEN_HELP = ("The input that carries the stream's sound - BlackHole, with a "
                   "Multi-Output Device as the system output. StreamSync hears it there, even "
                   "with the browser minimized.")
    DEFAULT_DEVICE_NOTE = "automatic"
    UPDATE_UNAVAILABLE = ("This copy is running from source, so it can't replace itself - "
                          "git pull instead.")

    # --- process and windows ----------------------------------------------

    @staticmethod
    def prepare_process():
        pass

    def _init_platform(self):
        self._swap_target = False    # what the last dispatched swap aims at
                                     # (after a failure, what is on screen)
        self._swap_seq = 0
        self._swap_app = ""          # resolved browser, cached across swaps
        self._swap_q = queue.Queue()
        self._fs_until = None        # a fullscreen request is being checked
        self._fs_retry = 0.0         # ...and is next looked at at this time
        # clear while a pause waits for the film to get out of fullscreen
        self._left_fullscreen = threading.Event()
        self._left_fullscreen.set()
        self._wheel_dir = None       # the scroll direction, read at the first wheel event

    def _init_root(self):
        root = self.root
        menubar = tk.Menu(root)
        apple = tk.Menu(menubar, name="apple", tearoff=0)

        def about():
            try:
                root.tk.call("tk::mac::standardAboutPanel")
            except Exception:
                pass
        apple.add_command(label="About StreamSync", command=about)
        menubar.add_cascade(menu=apple)
        root.config(menu=menubar)
        # Tk's own Command-Q exits through Tcl and skips _on_close, which
        # lost the settings and the watch party's goodbye; quit the way the
        # close box does instead.
        root.createcommand("tk::mac::Quit", self._on_close)
        root.createcommand("tk::mac::ShowPreferences", self._open_settings)
        # a click on the Dock icon brings back a closed main window
        root.createcommand("tk::mac::ReopenApplication", root.deiconify)

    def _start_platform(self):
        self._swap_thread = threading.Thread(target=self._swap_worker,
                                             daemon=True)
        self._swap_thread.start()

    def _close_platform(self):
        # Stop the swap worker first: raising or hiding a browser is not
        # wanted once we are going, and it must not front a closing app.
        # A pause still waiting for the film to leave fullscreen is called
        # off (the worker checks _swap_seq when its wait ends) and its wait
        # ended here: nothing pumps _settle_fullscreen for it any more.
        self._swap_seq += 1
        self._swap_q.put(None)
        self._left_fullscreen.set()

    def _present_root(self):
        # Same exposure the first-run dialog had: launched from Finder this
        # window can come up titled but unpainted until something forces a draw.
        depcheck.present_window(self.root)

    def _present(self, toplevel):
        # A Finder-launched app's dialogs can come up unpainted too - the
        # Update offer, which can open by itself just after launch, among them.
        depcheck.present_window(toplevel)
        # present_window leaves the window fixed at the size it had then.
        # Hand the size back to the content, where it is everywhere else:
        # an error line, or the Update dialog's next view, would otherwise
        # be cut off at the bottom. The window stays where it is.
        try:
            g = toplevel.geometry()
            at = re.search(r"[+-]", g).start()
            toplevel.geometry("")
            toplevel.geometry(g[at:])
        except Exception:
            pass                     # cosmetic: a window gone already keeps its size

    def _ui_scale(self):
        # Tk measures in points here, and winfo_fpixels("1i") is 72, not
        # 96: dividing by 96 as Windows does would shrink everything,
        # the fullscreen bar's width cap included.
        return 1.0

    def _ui_font_family(self):
        # "SF Pro" is not a Tk family; this is the system font, which is
        # what TkDefaultFont is.
        return ".AppleSystemUIFont"

    def _style_window(self, toplevel):
        # A dark title bar to match the window, whatever the system
        # appearance. Cosmetic only, and an unsupported command: never raises.
        try:
            toplevel.update_idletasks()
            toplevel.tk.call("::tk::unsupported::MacWindowStyle", "appearance",
                             toplevel._w, "darkaqua")
        except Exception:
            pass

    # --- seam 1: the picture ------------------------------------------------

    def _make_player(self):
        return EmbeddedPlayer(None)  # libvlc gets our NSView in _show_video_window

    def _show_video_window(self):
        """Show the video window and hand its view to libvlc, once.

        winfo_id() is only meaningful after the widget really exists on
        screen, so attachment happens here, at first show, rather than at
        construction while the window is still withdrawn.
        """
        self.video_win.deiconify()
        self.video_win.lift()
        self.video_win.update_idletasks()
        if not self.embedded.embedded:
            # the dialogs' dark title bar, set before libvlc has the view
            self._style_window(self.video_win)
            if not self.embedded.attach_tk(self.video_frame.winfo_id()):
                # The pre-1.0.5 behavior, named instead of silent: sound
                # with no picture reads as a broken film otherwise. The
                # full report goes to the log, and the status says where.
                diagnostics.log("attach_tk failed: no NSView from Tk; "
                                "playback continues without video")
                self._set_status("Video embedding is unavailable here - playback "
                                 "will be audio-only.")

                # The report runs Spotlight and codesign, which take seconds:
                # not on the Tk thread.
                def report():
                    where = diagnostics.log_report("attach_tk failed")
                    self.q.put(("status", "Video embedding is unavailable here - playback "
                                          f"will be audio-only. Details: {where}"))
                threading.Thread(target=report, daemon=True).start()

    # --- seam 2: fullscreen ---------------------------------------------------

    def _set_fullscreen(self, flag, show=True):
        """Fullscreen for the built-in player - the one way the app asks
        its way in or out. _settle_fullscreen sees that the window follows,
        so self.fullscreen says what the screen shows, or is about to.

        `show` is there for the shared signature: here the window is always
        shown. There is no early return when nothing changes, either:
        asking again is how a dropped request is put right."""
        if not flag:
            # the bar joins every Space: hide it before the window leaves,
            # and before _stream_swap queues the browser
            self._hide_fs_bar()
        self.fullscreen = bool(flag)
        if self.embedded.embedded:
            # The video lives in our own window now, so fullscreen is
            # the window's, not libvlc's - set_fullscreen only works
            # on a window libvlc itself owns.
            self.video_win.deiconify()
            self.video_win.attributes("-fullscreen", self.fullscreen)
            # ...which macOS may not have taken: _settle_fullscreen checks.
            now = time.monotonic()
            self._fs_retry = now + FULLSCREEN_RETRY
            self._fs_until = now + FULLSCREEN_SETTLE
        else:
            self.embedded.set_fullscreen(self.fullscreen)

    def _window_fullscreen(self):
        return bool(int(self.video_win.attributes("-fullscreen")))

    def _settle_fullscreen(self):
        """See that the film window is in the state last asked of it.

        The first run on a Mac with Automation refused (macOS 27): a pause
        took the film out of fullscreen, the swap failed 0.3 s later and
        fullscreen was handed back - while the window was still animating
        out. macOS dropped the request, so the film sat in a 960x540 window
        with self.fullscreen saying True until the next pause, which then
        "left" a fullscreen it was not in and "returned" to one for real.
        Any change asked for inside the animation goes the same way.

        So the window is asked what it is, and asked again for what was
        wanted until the two agree. Not straight after a request, though:
        for an instant after a dropped request to enter, Tk reports the
        window fullscreen (and screen-sized) before going back to the
        truth, so this check reads nothing until FULLSCREEN_RETRY after
        asking. (_stream_swap does read as it asks to leave - see there.)
        Asking again is safe: Tk only passes on a request that differs
        from the window's state, and macOS drops it again if it is still
        not ready. A window that never takes it is believed instead, so
        the next Toggle Fullscreen does what it says.
        """
        if self._fs_until is None:
            return
        now = time.monotonic()
        if now < self._fs_retry:
            return
        if self._window_fullscreen() == self.fullscreen:
            self._fs_until = None
        elif now >= self._fs_until:
            self._fs_until = None
            self.fullscreen = not self.fullscreen
        else:
            self._fs_retry = now + FULLSCREEN_RETRY
            self.video_win.attributes("-fullscreen", self.fullscreen)
            return
        self._left_fullscreen.set()
        if self.fullscreen:
            # settled in fullscreen, now that the screen shows it: the
            # film takes the keyboard (and, from Step 5, shows the bar)
            self._fullscreen_entered()

    def _fullscreen_from_window(self):
        # The green button and Ctrl-Cmd-F change the window behind the
        # app's back; a user's F or Esc goes by what the screen shows. Only
        # with no request in flight, which _settle_fullscreen is still
        # checking, and only once the film is in our window: before that,
        # fullscreen is libvlc's own, which the window knows nothing of.
        if self._fs_until is None and self.embedded.embedded:
            self.fullscreen = self._window_fullscreen()

    def _bar_allowed(self):
        # not while a request is still settling, nor over a film the
        # window says is not fullscreen
        return self._fs_until is None and self._window_fullscreen()

    def _bar_hold_stands(self):
        # A click on the bar never takes the foreground here, so a held bar
        # stays up. Step 5 revisits this with the bar on.
        return True

    def _no_activate(self, toplevel, hook, hwnd):
        # Nothing to do: a click on the bar or the OSD leaves the film the
        # key window here.
        return hook, hwnd

    def _wheel_sign(self):
        # Fingers up means louder. Natural scrolling (the default) turns
        # the deltas round, so read the setting once, at the first wheel
        # event; a missing key or any error counts as natural.
        if self._wheel_dir is None:
            natural = True
            try:
                r = subprocess.run(["defaults", "read", "-g",
                                    "com.apple.swipescrolldirection"],
                                   capture_output=True, text=True, timeout=2)
                natural = r.stdout.strip() != "0"
            except Exception:
                pass
            self._wheel_dir = -1 if natural else 1
        return self._wheel_dir

    # --- seam 3: the stream-pause swap (AppleScript) ----------------------------

    def _refresh_stream_list(self):
        def work():
            try:
                names = macwindowctl.list_gui_apps()
            except Exception as e:
                self.q.put(("status", f"Could not list apps: {e}"))
                return
            self.q.put(("apps", names))
        threading.Thread(target=work, daemon=True).start()

    def _on_stream_pick(self, _event=None):
        self.stream_window = self.stream_var.get()
        self._swap_app = ""          # resolve again against the new choice
        self._save_config()

    def _stream_swap(self, show):
        """Ask for the stream app to be shown (paused) or hidden (playing).

        Only the Tk half runs here. Each swap is 1-3 osascript round trips
        to System Events, any of which can block for seconds - the first
        waits on the Automation permission prompt - so they run on
        _swap_worker and come back through self.q as a "swapdone" event.
        """
        show = bool(show)
        if not self.swap_var.get() or not self.ctl.video_path \
                or show == self._swap_target:
            return
        self._swap_target = show
        self._swap_seq += 1
        embedded = self.player is self.embedded
        if show and embedded:
            if self.fullscreen:
                # Leave fullscreen before the browser is raised: a
                # fullscreen window would keep it behind the film. The
                # flag means "we owe the user fullscreen back", so it is
                # only ever set when we actually take it away - reading
                # self.fullscreen here would record False for a second
                # pause that arrives before the first one's restore has
                # run, and the debt would be forgotten.
                self._was_fullscreen = True
                self._set_fullscreen(False)
            if self._fs_until is not None and self._window_fullscreen():
                # macOS has not taken a request to leave: this pause's,
                # made while the film was still on its way into fullscreen
                # (a pause straight after a resume), or the user's own
                # Toggle Fullscreen a moment ago. The film leaves once
                # _settle_fullscreen asks again, and the browser must not
                # be raised before then - a window that leaves fullscreen
                # brings its app to the front, so the film would end up
                # over the browser raised a moment earlier. The worker
                # waits for this. A dropped request to leave reads true
                # straight away; if a request to enter was dropped an
                # instant ago this can read fullscreen when it is not,
                # and the pause then waits one FULLSCREEN_RETRY for
                # _settle_fullscreen to look.
                self._left_fullscreen.clear()
        elif not show and not self._left_fullscreen.is_set():
            # This resume has overtaken a pause that is still waiting for
            # the film to leave fullscreen. Call the leave off - the film
            # would drop out of fullscreen for a pause that is over - and
            # let the worker go: it sees that its pause was overtaken and
            # raises nothing.
            self._repay_fullscreen()
            self._left_fullscreen.set()
        self._swap_q.put((self._swap_seq, show, embedded))

    def _resolve_stream_app(self):
        """Worker thread: the app to swap with, "" if there is none."""
        if self.stream_window:
            return self.stream_window
        if not self._swap_app:
            # "every application process" costs 0.5-1.5 s, so hold on to
            # the answer. A miss is not kept, in case a browser opens later.
            running = macwindowctl.list_gui_apps()
            self._swap_app = next((b for b in BROWSERS if b in running), "")
        return self._swap_app

    def _swap_worker(self):
        while True:
            items = [self._swap_q.get()]
            # A rapid pause/resume only needs the state it settled on; drop
            # the swaps it passed through rather than play them back.
            while True:
                try:
                    items.append(self._swap_q.get_nowait())
                except queue.Empty:
                    break
            if None in items:
                return               # the app is closing
            seq, show, embedded = items[-1]
            try:
                app_name, why = self._resolve_stream_app(), None
            except Exception as e:
                # the lookup itself was refused: Automation permission
                app_name, why = "", e
            if not app_name:
                if show:
                    if why is not None:
                        self.q.put(("status", f"App swap failed: {why} (grant "
                                              "Automation permission in System "
                                              "Settings > Privacy)."))
                    else:
                        self.q.put(("status", "Pick the stream's browser in "
                                              "Settings > Stream app first."))
                # nothing was raised, and nothing we can reach is up
                self.q.put(("swapdone", seq, show, False, False))
                continue
            # Where this swap left the stream app; None until a call goes
            # through. A refused call moved nothing - a refused activate_app
            # raised nothing (at most it unhid the browser, and the film
            # stays in front), and a refused hide leaves it where the last
            # swap left it - so it stands as it was.
            shown = None
            try:
                if show:
                    # set, unless the film is still leaving fullscreen
                    self._left_fullscreen.wait(FULLSCREEN_SETTLE)
                    if seq != self._swap_seq:
                        # Overtaken while it waited, by a resume or by
                        # the app closing: the browser would come up over
                        # a film that is playing again. Whatever overtook
                        # it settles the screen.
                        continue
                    macwindowctl.activate_app(app_name)
                    shown = True
                else:
                    macwindowctl.hide_app(app_name)
                    shown = False
                    if embedded:
                        macwindowctl.activate_self()
                    else:
                        macwindowctl.activate_app("VLC")
            except Exception as e:
                self._swap_app = ""  # that app may have quit - look again
                self.q.put(("status", f"App swap failed: {e} (grant "
                                      "Automation permission in System "
                                      "Settings > Privacy)."))
                self.q.put(("swapdone", seq, show, False, shown))
                continue
            self.q.put(("swapdone", seq, show, True, shown))

    def _swap_done(self, seq, show, ok, shown):
        """Tk thread: settle a swap the worker has finished with. `shown`
        says whether it left the stream app up (None: as it was)."""
        if shown is not None:
            self._swapped = shown
        if seq != self._swap_seq:
            # A newer swap is already on its way and settles the film when
            # it lands. Giving fullscreen back now would put the film over
            # a browser that is about to be raised.
            return
        if not show or not ok:
            # The film is back, or the swap failed - a browser that never
            # came up, or one that would not hide again: either way
            # nothing else will hand fullscreen back.
            self._repay_fullscreen()
        if not ok:
            # Settle on what is on screen, so the next swap is judged
            # against that and not against what the failed one aimed at.
            # A fullscreen film is in front of whatever is still up, so the
            # stream's next pause has to raise it - after a hide that
            # failed and handed fullscreen back, too. A failed pause raised
            # nothing, so the resume every sync sends has nothing to do and
            # does not run the failing swap again. A hide that failed under
            # a windowed film leaves the stream app in front, and the next
            # resume tries it again.
            if self.fullscreen and self.player is self.embedded:
                self._swapped = False
            self._swap_target = self._swapped

    def _repay_fullscreen(self):
        if self._was_fullscreen and self.player is self.embedded:
            self._was_fullscreen = False   # debt paid
            self._set_fullscreen(True)

    # --- seam 4: installing an update (worker thread) ---------------------------

    def _installed_target(self):
        return updater.installed_app_path()

    def _install_update(self, path, target, say):
        staged = updater.stage(path, target, say)
        # the new bundle is staged beside the old one: the disk image and
        # its download folder are done with
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
        say("Restarting into the new version...")
        updater.swap_and_relaunch(target, staged)
