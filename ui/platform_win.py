"""The Windows adapter: what the shared shell does differently on Windows.

- The picture: libvlc draws straight into the film window's frame, whose
  window handle it is given when the player is made.
- Fullscreen is borderless, on the monitor the film window is on (Tk's own
  -fullscreen always picks the primary one), through windowctl.
- The fullscreen bar and the volume OSD are hooked so a click on them
  leaves the film the active window (windowctl.no_activate_on_click).
- The stream-pause swap restores the stream's browser window and
  minimizes it again, and puts the film's window back exactly where it
  was, through windowctl. Each step is a "swap: ..." line in the log.
- An update runs the downloaded installer, which takes over once the app
  has quit.

windowctl is imported at the top, and it calls ctypes.windll as it loads,
so only Windows imports this module (ui.shell.default_platform, and the
Windows-only tests); elsewhere it is read with ast.
"""

import traceback

import diagnostics
import picture_timing
import screencap
import windowctl
import updater
from players import EmbeddedPlayer
from ui import adapter

# the film window's Tk state, in the log's words
FILM_STATES = {"normal": "up", "zoomed": "maximized", "iconic": "minimized",
               "withdrawn": "closed"}


def _named(hwnd):
    """A window as the log names it: its handle, and its title now."""
    return f"{hwnd:#x} {windowctl.window_title(hwnd)!r}"


class Adapter(adapter.PlatformAdapter):
    NAME = "windows"
    FILM_CONTROLS = True
    FOCUS_FILM_ON_FULLSCREEN = False
    OVERLAYS_PER_FULLSCREEN = False
    WITHDRAW_FILM_FOR_EXTERNAL = True
    MENU_BUTTONS = ("<Button-3>",)
    SIDEWAYS_WHEEL = ()
    WHEEL_UNIT = 120
    WHEEL_QUIET = None
    FILM_GEOMETRY = (960, 540, (80, 80))
    OLD_STREAM_KEY = "stream_title"
    STREAM_LABEL = "Stream window"
    STREAM_AUTO_LABEL = "Automatic (Twitch, Kick or the only browser)"
    REFRESH_STREAMS_ON_SETTINGS = True
    LISTEN_HELP = ("The sound output the stream plays through. StreamSync hears it there, "
                   "even with the browser minimized.")
    DEFAULT_DEVICE_NOTE = "system default"
    UPDATE_UNAVAILABLE = ("This copy wasn't installed by the StreamSync installer, so it can't "
                          "update itself. Download the new version from the Releases page.")
    CAN_MEASURE_PICTURE = True

    # --- process and windows ----------------------------------------------

    @staticmethod
    def prepare_process():
        # make Tk report physical pixels, so window and monitor rectangles
        # agree with the ones windowctl gets from Win32
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass

    def _init_platform(self):
        self.stream_hwnd = None      # pinned stream browser window
        self._win_map = {}
        self._ext_hwnd = None
        self._fs_saved = None        # frame style + rect fullscreen replaced
        self._film_placement = None  # where the swap hid the film window from
        self._measure_restore = False  # minimize the stream again after timing it

    def _init_root(self):
        pass                         # no app menu or application hooks here

    def _start_platform(self):
        self._refresh_stream_list()

    def _close_platform(self):
        pass                         # nothing runs in the background here

    def _present_root(self):
        # A restored film's window is shown while the app is built, and it
        # can come up over the main window: put the main window back on
        # top, with the keyboard.
        try:
            self.root.update_idletasks()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def _present(self, toplevel):
        pass                         # a new Toplevel draws itself here

    def _ui_scale(self):
        # Under the DPI awareness prepare_process asks for, Tk reports
        # physical pixels: 96 to the inch at 100% scaling, 144 at 150%.
        try:
            return self.root.winfo_fpixels("1i") / 96
        except Exception:
            return 1.0

    def _ui_font_family(self):
        return "Segoe UI"

    def _style_window(self, toplevel):
        # A dark title bar to match the window: DWMWA_USE_IMMERSIVE_DARK_MODE,
        # attribute 20 from Windows 10 20H1 on and 19 before it. Cosmetic
        # only: never raises.
        try:
            import ctypes
            toplevel.update_idletasks()
            hwnd = int(toplevel.frame(), 16)
            on = ctypes.c_int(1)
            for attr in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                    break
        except Exception:
            pass

    # --- seam 1: the picture ------------------------------------------------

    def _make_player(self):
        # the constructor turns libvlc's mouse and key input off, so clicks
        # and keys over the picture reach the film window
        return EmbeddedPlayer(self.video_frame.winfo_id())

    def _show_video_window(self):
        self._raise_film()
        self.video_win.lift()
        # its title bar too; a window's frame only exists once it has shown
        self._style_window(self.video_win)

    def _raise_film(self):
        # Tk's deiconify shows even a window that is up (SW_SHOWNOACTIVATE),
        # and that un-snaps it: a film snapped to half a monitor jumps back
        # to its size from before the snap (test_windowctl measures it). A
        # film window that is up is raised and given the keyboard instead,
        # as deiconify would; a hidden or minimized one is deiconified.
        if self.video_win.state() in ("normal", "zoomed"):
            self.video_win.lift()
            self.video_win.focus_force()
        else:
            self.video_win.deiconify()

    # --- seam 2: fullscreen ---------------------------------------------------

    def _set_fullscreen(self, flag, show=True):
        if show:
            self._raise_film()
        if flag == self.fullscreen:
            return
        # Tk's -fullscreen always picks the primary monitor; cover the one
        # the window is on, as VLC does (see windowctl). A window shown
        # just now gets its OS frame at idle time, so let that happen.
        self.video_win.update_idletasks()
        hwnd = int(self.video_win.frame(), 16)
        if flag:
            self._fs_saved = windowctl.borderless_fullscreen(hwnd)
            self.video_win.lift()
            self._fullscreen_entered()
        else:
            if self._fs_saved is not None:
                windowctl.unfullscreen(hwnd, self._fs_saved)
            self._fs_saved = None
            self._hide_fs_bar()
        self.fullscreen = flag

    def _fullscreen_from_window(self):
        pass                         # only the app changes the frame style here

    def _bar_allowed(self):
        return True

    def _bar_hold_stands(self):
        # A resync from the bar holds it up until the result is in - unless
        # another application has the keyboard on the film's monitor: after
        # an Alt+Tab to the browser there, the topmost bar would sit over
        # it for the whole sync, so it rests as usual (hiding it outright
        # would flicker it under a mouse crossing the film). A window on
        # another monitor having the keyboard is the usual case, not that:
        # a click on the bar leaves the foreground where it was (see
        # windowctl.no_activate_on_click).
        fg = windowctl.foreground()
        film = int(self.video_win.frame(), 16)
        return (not fg
                or fg in (film, self._fs_hwnd, int(self.root.frame(), 16))
                or windowctl.monitor_rect(fg)
                != windowctl.monitor_rect(film))

    def _no_activate(self, toplevel, hook, hwnd):
        # a click on it must leave the film the active window (see
        # windowctl.no_activate_on_click)
        h = int(toplevel.frame(), 16)
        if h != hwnd:                # first showing, or Tk rebuilt it
            hook = windowctl.no_activate_on_click(h)
            hwnd = h
        return hook, hwnd

    def _wheel_sign(self):
        return 1

    # --- seam 3: the stream-pause swap --------------------------------------------

    def _refresh_stream_list(self):
        # No pick is made here: "" is Automatic, and find_stream_window
        # falls back to any Twitch or Kick window, or to the one browser
        # window open, when the swap runs.
        # Picking one here pinned a title at every launch and every
        # Settings open - undoing an explicit Automatic - and saved the
        # settings with nobody asking.
        wins = [(h, t) for h, t in windowctl.list_windows()
                if t.strip() and not t.startswith("StreamSync")]
        self._win_map = {t[:70]: h for h, t in wins}
        self._apply_stream_list(list(self._win_map.keys()))

    def _on_stream_pick(self, _event=None):
        title = self.stream_var.get()
        self.stream_hwnd = self._win_map.get(title)
        self.stream_window = title
        self._save_config()

    # --- seam 3b: picture timing -------------------------------------------

    def _picture_tools(self):
        # libvlc draws the film in a child of the frame, sized to the
        # picture; the frame is the fallback. Win32 only from here on, so
        # the controller's threads may call it.
        frame = self.video_frame.winfo_id()

        def film_picture_rect():
            if not windowctl.is_visible(frame):
                return None
            return (windowctl.child_rect(frame, "VLC video output")
                    or windowctl.client_rect(frame))
        return screencap.grab, film_picture_rect

    def _stream_for_measure(self):
        hwnd = windowctl.find_stream_window(self.stream_hwnd, self.stream_window)
        if hwnd is None:
            return None, "", ("Couldn't find the stream window - pick it in Settings > "
                              "Stream window (Refresh lists what's open).")
        self.stream_hwnd = hwnd
        # A browser draws nothing while minimized or covered, so it comes up
        # for the measurement; minimized before, it goes back after.
        self._measure_restore = windowctl.is_minimized(hwnd)
        windowctl.bring_forward(hwnd)
        diagnostics.log(f"picture: timing the stream window {_named(hwnd)}")

        def stream_rect():
            if (not windowctl.is_valid(hwnd) or windowctl.is_minimized(hwnd)
                    or not windowctl.is_visible(hwnd)):
                return None
            return windowctl.client_rect(hwnd)
        return stream_rect, picture_timing.channel_key(windowctl.window_title(hwnd)), ""

    def _measure_done(self):
        hwnd, self._measure_restore = self.stream_hwnd if self._measure_restore else None, False
        if hwnd and windowctl.is_valid(hwnd) and not self._swapped:
            windowctl.minimize(hwnd)

    def _note_stream(self):
        hwnd = windowctl.find_stream_window(self.stream_hwnd, self.stream_window)
        if hwnd:
            self.ctl.set_stream_key(picture_timing.channel_key(windowctl.window_title(hwnd)))

    def _stream_swap(self, show, why=""):
        """Show the stream's browser window during pauses; hide it again after."""
        if not self.swap_var.get() or show == self._swapped or not self.ctl.video_path:
            return
        # A line for each step: on the PC, the browser was minimized during
        # a pause, and with nothing in the log nobody could tell whether a
        # swap had done it.
        diagnostics.log(f"swap: {'pause' if show else 'resume'}"
                        + (f" ({why})" if why else ""))
        try:
            if show:
                hwnd = windowctl.find_stream_window(self.stream_hwnd,
                                                    self.stream_window)
                if hwnd is None:
                    diagnostics.log("swap: no stream window found")
                    self._set_status("Couldn't find the stream window - pick it in "
                                     "Settings > Stream window (Refresh lists what's open).")
                    return
                self.stream_hwnd = hwnd
                if self.player is self.embedded:
                    self._swap_hide_film()
                elif self.external is not None and self.external.proc:
                    self._ext_hwnd = windowctl.find_by_pid(self.external.proc.pid)
                    if self._ext_hwnd:
                        windowctl.minimize(self._ext_hwnd)
                        diagnostics.log(f"swap: VLC window {_named(self._ext_hwnd)} "
                                        "minimized")
                    else:
                        diagnostics.log("swap: no VLC window found to minimize")
                windowctl.restore(hwnd)
                diagnostics.log(f"swap: stream window {_named(hwnd)} restored")
                self._swapped = True
            else:
                if self.stream_hwnd and windowctl.is_valid(self.stream_hwnd):
                    windowctl.minimize(self.stream_hwnd)
                    diagnostics.log(f"swap: stream window {_named(self.stream_hwnd)} "
                                    "minimized")
                else:
                    diagnostics.log("swap: the stream window has gone; nothing to minimize")
                saved, self._film_placement = self._film_placement, None
                if self.player is self.embedded:
                    self._swap_show_film(saved)
                elif self._ext_hwnd and windowctl.is_valid(self._ext_hwnd):
                    windowctl.restore(self._ext_hwnd)
                    diagnostics.log(f"swap: VLC window {_named(self._ext_hwnd)} restored")
                self._swapped = False
        except Exception as e:
            diagnostics.log_block("swap failed:", traceback.format_exc())
            self._set_status(f"Window swap failed: {e}")

    def _swap_hide_film(self):
        # Only a film window that is up is hidden: one the user closed or
        # minimized stays that way through the pause and after it.
        state = self.video_win.state()
        shown = state in ("normal", "zoomed")
        how = FILM_STATES.get(state, state)
        # resuming puts it back fullscreen, on the same monitor
        self._was_fullscreen = self.fullscreen and shown
        # Leaving fullscreen puts back the frame and rectangle it had
        # before; a film not fullscreen is not touched (no show, no
        # change). Its placement is read after that, from the window as
        # it was before fullscreen.
        if self.fullscreen:
            self._set_fullscreen(False, show=False)
            diagnostics.log("swap: fullscreen left")
        if shown:
            self._film_placement = windowctl.placement(int(self.video_win.frame(), 16))
            self.video_win.withdraw()
            # placement() is (WINDOWPLACEMENT, the rectangle it was at)
            diagnostics.log(f"swap: film window hidden, was {how} "
                            f"at {self._film_placement[1]}")
        else:
            diagnostics.log(f"swap: film window left as it is ({how})")

    def _swap_show_film(self, saved):
        # deiconify alone does not bring it back where it was: a window
        # snapped to half a monitor comes back at its size from before the
        # snap (see windowctl.placement). Tk's own geometry work is done
        # first, so none of it runs after the placement is put back. A
        # film the user brought back during the pause is left where it is.
        state = self.video_win.state()
        if saved is not None and state == "withdrawn":
            self.video_win.deiconify()
            self.video_win.update_idletasks()
            windowctl.put_placement(int(self.video_win.frame(), 16), saved)
            diagnostics.log(f"swap: film window shown again at {saved[1]}")
        else:
            diagnostics.log("swap: film window left as it is "
                            f"({FILM_STATES.get(state, state)})")
        if self._was_fullscreen:
            self._was_fullscreen = False
            self._set_fullscreen(True)
            diagnostics.log("swap: fullscreen given back")

    # --- seam 4: installing an update (worker thread) ---------------------------------

    def _installed_target(self):
        # None for a copy the installer did not put there (a source
        # checkout, an unzipped build): it can only point at the Releases page.
        return updater.installed_exe_path()

    def _install_update(self, path, target, say):
        # The installer runs from `path`'s folder once the app has quit,
        # which the update flow does through _on_close as soon as this
        # returns (settings saved, the watch party told).
        say("Starting the installer...")
        updater.install_and_relaunch_windows(path)
