"""What differs between Windows and the Mac, as one contract.

The shell is a set of mixins that ui/shell.py composes into one App class:

    App(Adapter, FilmWindow, MainWindow, UpdateFlow, Shell)

Adapter is ui/platform_win.Adapter or ui/platform_mac.Adapter, a subclass
of PlatformAdapter below. It comes first, so its seams and constants win
over the shared ones. PlatformAdapter is Shell's base and so comes last:
every method here that has no default raises NotImplementedError with its
own name, and an adapter that forgot one fails loudly where it is called
instead of doing nothing.

Every method runs on the Tk thread, except where its docstring says it
runs on a worker thread. No tkinter here: tests read the contract with no
display.
"""


class PlatformAdapter:
    """What differs between Windows and the Mac. A platform's Adapter
    subclass is mixed in first, so it wins; this base sits last."""

    # --- constants -------------------------------------------------------
    # The values here are Windows'; an adapter overrides what differs, and
    # must set every one that is empty here.

    NAME = ""                          # "windows" or "mac"
    # Whether the film window takes the mouse - double-click, wheel, the
    # right-click menu - and shows the fullscreen bar. Both platforms do;
    # it is the one switch that turns them off on a platform where they
    # misbehave. Keys work either way.
    FILM_CONTROLS = True
    # Give the film window the keyboard once fullscreen settles. On the
    # Mac, entering fullscreen does not make the film's window key, so
    # Space, F and Esc over the film would go nowhere.
    FOCUS_FILM_ON_FULLSCREEN = False
    # Hide the built-in player's film window on a switch to the VLC app.
    # The Mac has always kept it up.
    WITHDRAW_FILM_FOR_EXTERNAL = True
    # The mouse buttons that open the film menu. Aqua's right button is
    # Button-2, and a one-button mouse sends Control-click.
    MENU_BUTTONS = ("<Button-3>",)
    # Wheel sequences the film window swallows: Aqua sends sideways
    # scrolling as Shift-wheel, which would otherwise step the volume.
    SIDEWAYS_WHEEL = ()
    # Wheel delta per volume step (a Windows mouse notch is 120), and the
    # rest in seconds after which the next wheel event steps at once - for
    # the Mac, where every scroll is a stream of small deltas and a single
    # flick must still do something (None: no such rest).
    WHEEL_UNIT = 120
    WHEEL_QUIET = None
    # The film window's size and position in design px: (width, height,
    # (x, y)), or (width, height, None) to let the window manager place it.
    FILM_GEOMETRY = (960, 540, (80, 80))
    # The key 1.x saved the stream pick under; read once by _load_config.
    OLD_STREAM_KEY = ""                # "stream_title" or "stream_app"
    STREAM_LABEL = ""                  # Settings: "Stream window" or "Stream app"
    STREAM_AUTO_LABEL = ""             # how the "" (Automatic) pick reads there
    # Refresh the stream list each time Settings opens. Listing windows is
    # free on Windows; listing the Mac's apps goes through System Events,
    # which asks for Automation permission, so there it waits for Refresh.
    REFRESH_STREAMS_ON_SETTINGS = True
    LISTEN_HELP = ""                   # Settings: the help under "Listen to"
    DEFAULT_DEVICE_NOTE = ""           # "(default) X" reads "X (<note>)"
    UPDATE_UNAVAILABLE = ""            # why this copy can't update itself

    # --- process and windows ---------------------------------------------

    @staticmethod
    def prepare_process():
        """Process-wide setup, called by main() before tk.Tk(). Windows
        makes the process DPI aware here, so Tk reports physical pixels."""
        raise NotImplementedError("prepare_process")

    def _init_platform(self):
        """The adapter's own state. Shell.__init__ calls this first, before
        any widget exists: no Tk calls."""
        raise NotImplementedError("_init_platform")

    def _init_root(self):
        """Menus and application hooks on the root window, once it has its
        title and theme (the Mac's app menu and tk::mac commands)."""
        raise NotImplementedError("_init_root")

    def _start_platform(self):
        """The last step of construction, after ctl.start()."""
        raise NotImplementedError("_start_platform")

    def _close_platform(self):
        """The first thing _on_close does once it has set _closing, before
        the update is cancelled, the settings saved and the controller
        stopped."""
        raise NotImplementedError("_close_platform")

    def _present_root(self):
        """Called by main() once the app is built: the main window on top,
        with the keyboard."""
        raise NotImplementedError("_present_root")

    def _present(self, toplevel):
        """Every dialog calls this once it is built and has registered
        itself on the app. Default: nothing."""

    def _ui_scale(self):
        """Screen px per design px: the theme's scale, which also scales
        the fullscreen bar's width cap. Default 1.0."""
        return 1.0

    def _ui_font_family(self):
        """The font family theme.font() uses."""
        raise NotImplementedError("_ui_font_family")

    def _style_window(self, toplevel):
        """Optional platform styling for a new toplevel, such as a dark
        title bar. It must never raise. Default: nothing."""

    # --- seam 1: the picture ----------------------------------------------

    def _make_player(self):
        """Build the EmbeddedPlayer, once, in Shell.__init__ after the film
        window exists. VLCError propagates to the caller."""
        raise NotImplementedError("_make_player")

    def _show_video_window(self):
        """Show and lift the film window. On the Mac this also hands libvlc
        the window's view, once."""
        raise NotImplementedError("_show_video_window")

    # --- seam 2: fullscreen -----------------------------------------------

    def _set_fullscreen(self, flag, show=True):
        """Put the built-in player's film window into fullscreen (flag
        true) or take it out, keeping self.fullscreen in step. show=False
        leaves a hidden film window hidden where the platform can."""
        raise NotImplementedError("_set_fullscreen")

    def _settle_fullscreen(self):
        """Called on every _poll_queue pass while the app runs: see that the
        film window took the last request. Default: nothing."""

    def _fullscreen_from_window(self):
        """Called before a user's F, Esc or fullscreen toggle, and when the
        bar finds it may not show (_bar_allowed false): bring
        self.fullscreen in line with the window, which the system can
        change behind the app's back. Default: nothing."""

    def _bar_allowed(self):
        """May the fullscreen bar show now? -> bool"""
        raise NotImplementedError("_bar_allowed")

    def _bar_hold_stands(self):
        """While a resync started from the bar holds it up (_fs_hold),
        should it stay up? -> bool"""
        raise NotImplementedError("_bar_hold_stands")

    def _no_activate(self, toplevel, hook, hwnd):
        """Keep a click on `toplevel` (the bar, the OSD) from taking the
        foreground from the film. `hook` and `hwnd` are what the last call
        returned for this toplevel (None at first); returns the pair to
        keep, which the caller holds on to for as long as the toplevel
        lives."""
        raise NotImplementedError("_no_activate")

    def _wheel_sign(self):
        """+1 or -1: the sign that makes the wheel's (or the trackpad's)
        "up" turn the volume up."""
        raise NotImplementedError("_wheel_sign")

    # --- seam 3: the stream-pause swap ------------------------------------

    def _refresh_stream_list(self):
        """List what the stream could be playing in: windows on Windows,
        apps on the Mac. Ends in self._apply_stream_list(items), directly
        or through the queue as ("apps", items)."""
        raise NotImplementedError("_refresh_stream_list")

    def _on_stream_pick(self, _event=None):
        """The stream pick in Settings changed: stream_var becomes
        self.stream_window ("" is Automatic)."""
        raise NotImplementedError("_on_stream_pick")

    def _stream_swap(self, show):
        """Bring the stream forward while the film is paused (show true),
        and the film back when it plays again. Synchronous on the Tk side;
        anything that can block for long (the Mac's osascript round trips)
        runs on a worker thread and reports back through the queue."""
        raise NotImplementedError("_stream_swap")

    # --- seam 4: installing an update (worker thread) ---------------------

    def _installed_target(self):
        """What an update would replace: a path, or None when this copy
        cannot update itself (run from source, or not installed by the
        installer)."""
        raise NotImplementedError("_installed_target")

    def _install_update(self, path, target, say):
        """Worker thread: install the downloaded and verified file at
        `path` over `target`, calling say(text) for each step. Returning
        means the new version takes over once the app quits; raising
        means nothing was changed."""
        raise NotImplementedError("_install_update")
