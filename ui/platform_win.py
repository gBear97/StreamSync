"""The Windows adapter: what the shared shell does differently on Windows.

- The picture: libvlc draws straight into the film window's frame, whose
  window handle it is given when the player is made.
- Fullscreen is borderless, on the monitor the film window is on (Tk's own
  -fullscreen always picks the primary one), through windowctl.
- The fullscreen bar and the volume OSD are hooked so a click on them
  leaves the film the active window (windowctl.no_activate_on_click).
- The stream-pause swap restores the stream's browser window and
  minimizes it again, through windowctl.
- An update runs the downloaded installer, which takes over once the app
  has quit.

windowctl is imported at the top, and it calls ctypes.windll as it loads,
so only Windows imports this module (ui.shell.default_platform, and the
Windows-only tests); elsewhere it is read with ast.
"""

import windowctl
import updater
from players import EmbeddedPlayer
from ui import adapter


class Adapter(adapter.PlatformAdapter):
    NAME = "windows"
    FILM_CONTROLS = True
    FOCUS_FILM_ON_FULLSCREEN = False
    WITHDRAW_FILM_FOR_EXTERNAL = True
    MENU_BUTTONS = ("<Button-3>",)
    SIDEWAYS_WHEEL = ()
    WHEEL_UNIT = 120
    WHEEL_QUIET = None
    FILM_GEOMETRY = (960, 540, (80, 80))
    OLD_STREAM_KEY = "stream_title"
    STREAM_LABEL = "Stream window"
    STREAM_AUTO_LABEL = "Automatic (Twitch or Kick window)"
    REFRESH_STREAMS_ON_SETTINGS = True
    LISTEN_HELP = ("The sound output the stream plays through. StreamSync hears it there, "
                   "even with the browser minimized.")
    DEFAULT_DEVICE_NOTE = "system default"
    UPDATE_UNAVAILABLE = ("This copy wasn't installed by the StreamSync installer, so it can't "
                          "update itself. Download the new version from the Releases page.")

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
        self.video_win.deiconify()
        self.video_win.lift()
        # its title bar too; a window's frame only exists once it has shown
        self._style_window(self.video_win)

    # --- seam 2: fullscreen ---------------------------------------------------

    def _set_fullscreen(self, flag, show=True):
        if show:
            self.video_win.deiconify()
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
        # falls back to any Twitch or Kick window when the swap runs.
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

    def _stream_swap(self, show):
        """Show the stream's browser window during pauses; hide it again after."""
        if not self.swap_var.get() or show == self._swapped or not self.ctl.video_path:
            return
        try:
            if show:
                hwnd = windowctl.find_stream_window(self.stream_hwnd,
                                                    self.stream_window)
                if hwnd is None:
                    self._set_status("Couldn't find the stream window - pick it in "
                                     "Settings > Stream window (Refresh lists what's open).")
                    return
                self.stream_hwnd = hwnd
                if self.player is self.embedded:
                    self._was_fullscreen = self.fullscreen
                    # resuming puts it back fullscreen, on the same monitor
                    self._set_fullscreen(False, show=False)
                    self.video_win.withdraw()
                elif self.external is not None and self.external.proc:
                    self._ext_hwnd = windowctl.find_by_pid(self.external.proc.pid)
                    if self._ext_hwnd:
                        windowctl.minimize(self._ext_hwnd)
                windowctl.restore(hwnd)
                self._swapped = True
            else:
                if self.stream_hwnd and windowctl.is_valid(self.stream_hwnd):
                    windowctl.minimize(self.stream_hwnd)
                if self.player is self.embedded:
                    if self._was_fullscreen:
                        self._set_fullscreen(True)
                    else:
                        self.video_win.deiconify()
                elif self._ext_hwnd and windowctl.is_valid(self._ext_hwnd):
                    windowctl.restore(self._ext_hwnd)
                self._swapped = False
        except Exception as e:
            self._set_status(f"Window swap failed: {e}")

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
