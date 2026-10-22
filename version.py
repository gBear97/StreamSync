"""Single source of truth for StreamSync's version.

Bump __version__ here and everything downstream follows: the version
shown in the main window's footer and the Update dialog, the version the
updater compares releases against, the built .app's Info.plist, the
Windows installer's version, and the names of the files CI publishes.
A release's tag must match it (v + this), or the Release workflow stops.
"""

__version__ = "2.0.7"
