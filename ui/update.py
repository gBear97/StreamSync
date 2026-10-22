"""Updates: check, offer, download, verify, install through the platform.

UpdateFlow is the App's update mixin. A check runs on a thread and always
answers through the queue; a newer release opens the Update dialog
(ui/dialogs.py), quiet or not, and every answer goes to the Updates line
in Settings. No messagebox is left in the flow, so _poll_queue never sits
under a native modal.

Installing is the platform's seam (_installed_target, _install_update):
the Mac stages the new bundle and swaps it in after we quit; Windows runs
the installer. This module downloads and verifies, on a worker thread
that talks to the Tk thread only through self.q, and closes the app the
normal way once the platform has started the new version.

updater is looked up at call time, so tests can swap it. A download is
trusted only once SHA256SUMS.sig checks out against the release key
built into updater and the file's hash is in those signed sums.
"""

import shutil
import tempfile
import threading
import webbrowser

import updater
from ui import dialogs
from version import __version__


class UpdateCancelled(Exception):            # not an OSError: download_verified must not wrap it
    pass


class UpdateFlow:
    """The update flow's state, its actions and its queue handlers."""

    def _init_updates(self):
        self._update_checking = False
        self._update_offer = None            # (tag, rel) from the last check that found one
        self._update_running = False
        self._update_cancel = threading.Event()
        self._update_line = f"StreamSync {__version__}"

    # --- checking --------------------------------------------------------

    def _check_updates(self, quiet=True):
        """Ask GitHub for a newer release, off the UI thread.

        quiet=True means only that nobody asked (the startup check). Every
        answer goes to the Settings line either way, and a newer release
        opens its offer either way: there is no dialog to nag with when
        there is nothing new, and a laptop with no network just shows why
        in Settings."""
        if self._update_checking:
            return                           # one at a time: two would give two offers
        self._update_checking = True
        self._update_line = "Checking for updates…"
        self._refresh_update_line()

        def work():
            try:
                tag, rel = updater.available_update()
            except Exception as e:
                self.q.put(("update_err", str(e)))
                return
            if tag:
                self.q.put(("update", tag, rel))
            else:
                self.q.put(("update_none", __version__))

        threading.Thread(target=work, daemon=True).start()

    def _refresh_update_line(self):
        """The Updates row in Settings, if it is open: the line, and what
        its button does - nothing while a check runs, Update... once one
        found a release, else Check now."""
        if self._settings is not None:
            action = (None if self._update_checking
                      else "update" if self._update_offer else "check")
            self._settings.set_update_line(self._update_line, action)

    def _show_update_offer(self):
        if self._update_dialog is not None:
            self._update_dialog.lift()
        elif self._update_offer is not None:
            self._open_update_dialog(*self._update_offer)
        else:
            self._check_updates(quiet=False)

    def _open_update_dialog(self, tag, rel):
        dialogs.UpdateDialog(self, tag, rel,
                             can_install=self._installed_target() is not None,
                             unavailable_text=self.UPDATE_UNAVAILABLE)

    # --- installing --------------------------------------------------------

    def _start_update(self, tag, rel):
        if self._update_running:
            return
        target = self._installed_target()
        if target is None:                   # the offer said so already
            return
        self._update_running = True
        self._update_cancel.clear()
        if self._update_dialog is not None:
            self._update_dialog.show_progress(0, 0, "")

        def work():
            tmp = tempfile.mkdtemp(prefix="streamsync-dl-")
            shown = [None]
            try:
                # raises UpdateError when no asset fits this machine
                asset = updater.pick_asset(rel.get("assets", []))

                def progress(done, total):
                    if self._update_cancel.is_set():
                        raise UpdateCancelled()
                    pct = done * 100 // total if total else done // (1 << 20)
                    if pct != shown[0]:              # one event per percent (or MB)
                        shown[0] = pct
                        self.q.put(("update_progress", done, total, asset["name"]))

                path = self._download_update(asset, rel, tmp, progress)
                if self._update_cancel.is_set():
                    raise UpdateCancelled()
                self.q.put(("update_installing",))   # Cancel is disabled from here
                self.q.put(("update_step", self._verified_text()))
                self._install_update(path, target, say=lambda t: self.q.put(("update_step", t)))
                self.q.put(("update_ready",))
            except UpdateCancelled:
                shutil.rmtree(tmp, ignore_errors=True)
                self.q.put(("update_cancelled",))
            except Exception as e:
                # Bound now: Python deletes `e` when this block ends, and the
                # dialog runs later - it used to die with a NameError instead
                # of saying why the update failed.
                why = str(e)
                shutil.rmtree(tmp, ignore_errors=True)
                self.q.put(("update_failed", why))

        # On success tmp stays: Windows' installer runs from it after we
        # quit, and the Mac's install removes its own disk image. The new
        # version clears what is left as it starts (updater.sweep_downloads,
        # from streamsync.py), once the installer has let go of it.
        threading.Thread(target=work, daemon=True).start()

    def _download_update(self, asset, rel, dest, progress):
        """Download and verify the asset into dest (worker thread)."""
        # checks SHA256SUMS.sig with the release key, then the file
        return updater.download_verified(asset, rel, dest, progress)

    def _verified_text(self):
        return "Checked against the release's signed checksums. Installing..."

    def _cancel_update(self):
        self._update_cancel.set()

    # --- the release pages ----------------------------------------------------

    def _open_release_notes(self, rel):
        # Only ever a GitHub page: anything else in the feed reads as the
        # Releases page.
        url = (rel or {}).get("html_url") or ""
        webbrowser.open(url if url.startswith("https://github.com/") else updater.RELEASES_PAGE)

    def _open_releases_page(self):
        webbrowser.open(updater.RELEASES_PAGE)

    # --- queue handlers ------------------------------------------------------

    def _on_update(self, tag, rel):
        self._update_checking = False
        self._update_offer = (tag, rel)
        self._update_line = f"StreamSync {tag.lstrip('vV')} is available."
        self._refresh_update_line()
        if self._update_dialog is None and not self._update_running:
            self._open_update_dialog(tag, rel)

    def _on_update_none(self, version):
        self._update_checking = False
        self._update_offer = None
        self._update_line = f"StreamSync {version} is up to date."
        self._refresh_update_line()

    def _on_update_err(self, text):
        self._update_checking = False
        self._update_line = f"Couldn't check for updates: {text}"
        self._refresh_update_line()

    def _on_update_progress(self, done, total, name):
        if self._update_dialog is not None:
            self._update_dialog.show_progress(done, total, name)

    def _on_update_step(self, text):
        if self._update_dialog is not None:
            self._update_dialog.show_step(text)

    def _on_update_installing(self):
        if self._update_dialog is not None:
            self._update_dialog.set_installing()

    def _on_update_ready(self):
        # The normal close: settings saved, the session's goodbye, the
        # engine stopped. main() then ends the process at once, so neither
        # the Mac's swap helper nor the Windows installer waits on libvlc.
        self._on_close()

    def _on_update_failed(self, why):
        self._update_running = False
        if self._update_dialog is None and self._update_offer is not None:
            self._open_update_dialog(*self._update_offer)
        if self._update_dialog is not None:
            self._update_dialog.show_failed(why)

    def _on_update_cancelled(self):
        self._update_running = False
        if self._update_dialog is not None:
            self._update_dialog.close()
