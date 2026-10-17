"""StreamSync's window: one Tk shell for Windows and macOS.

ui.shell builds the app from mixins - the main window (mainwin), the film
window with its menu and fullscreen bar (video), the update flow (update)
and the shared logic (shell) - on top of a platform adapter (platform_win
or platform_mac) that holds everything the two systems do differently.
adapter.py is that contract, theme.py the design's colours and sizes,
widgets.py and icons.py the controls drawn to match it, and contract.py the
table of which mixin owns each method the others call.

Run it with `python -m ui.shell`, or through streamsync.py, which checks
the dependencies first.

Nothing is imported here, so `import ui` is safe anywhere, headless
included.
"""
