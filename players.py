"""Playback backends.

EmbeddedPlayer -- libvlc rendering into our own window; millisecond seeks.
ExternalPlayer -- drives the real VLC app through its HTTP interface. That
interface only seeks in whole seconds, so sync_seek waits until the moving
target crosses the next whole second and fires the seek exactly then, and
sub-second nudges are done as short playback-rate pulses (smooth, and
inaudible since the local copy is muted).
"""

import base64
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import vlc
    _IMPORT_ERROR = None
except Exception as e:  # missing libvlc.dll, bitness mismatch, etc.
    vlc = None
    _IMPORT_ERROR = e

EMBED_SEEK_LATENCY = 0.25   # seconds embedded VLC needs to execute a seek
# libvlc's get_time() reports a position behind the audio it is actually
# emitting (measured on macOS: 171 ms, spread 155-178, by matching its own
# output recorded off BlackHole against the file). Seeks are unaffected -
# they are timed from the capture, not from this clock - but anything
# comparing the clock to the stream must add this back, or a film that is
# perfectly in sync looks like it has drifted by this much.
CLOCK_OUTPUT_LAG = 0.17 if sys.platform == "darwin" else 0.0
EXT_CMD_LEAD = 0.12         # fire external seeks this early (http + exec time)
EXT_PORT = 9723
EXT_PASSWORD = "streamsync"

if sys.platform == "darwin":
    VLC_EXE_CANDIDATES = [
        "/Applications/VLC.app/Contents/MacOS/VLC",
        os.path.expanduser("~/Applications/VLC.app/Contents/MacOS/VLC"),
    ]
else:
    VLC_EXE_CANDIDATES = [
        r"C:\Program Files\VideoLAN\VLC\vlc.exe",
        r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
    ]


class VLCError(RuntimeError):
    pass


class EmbeddedPlayer:
    def __init__(self, hwnd=None, nsview=None):
        """hwnd: Tk window handle to render into (Windows).
        nsview: NSView pointer to render into (macOS - see macvideo.py).

        Exactly one of them carries the drawable for the platform in use.
        libvlc on macOS will not open a window of its own: with neither
        set, playback runs but nothing is ever displayed."""
        if vlc is None:
            raise VLCError(
                "Could not load VLC (libvlc).\n\n"
                "Install VLC from videolan.org (64-bit on Windows, matching "
                "your Python).\n\nDetails: %s" % _IMPORT_ERROR)
        self.instance = vlc.Instance("--no-video-title-show", "--quiet")
        if self.instance is None:
            raise VLCError("Could not create a VLC instance.")
        self.mp = self.instance.media_player_new()
        self.nsview = nsview
        if nsview is not None:
            self.mp.set_nsobject(int(nsview))
        elif hwnd is not None:
            self.mp.set_hwnd(int(hwnd))
            # let Tk keep mouse/keyboard events, not the VLC child window
            self.mp.video_set_mouse_input(False)
            self.mp.video_set_key_input(False)
        self.has_media = False

    def load(self, path):
        self.mp.set_media(self.instance.media_new(path))
        self.has_media = True

    def ensure_playing(self, timeout=6.0):
        if not self.has_media:
            raise VLCError("No video file loaded.")
        if not self.mp.is_playing():
            self.mp.play()
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if self.mp.is_playing() and self.mp.get_time() >= 0:
                return
            time.sleep(0.05)
        raise VLCError("VLC did not start playback in time.")

    def sync_seek(self, match_t, t0_perf, offset):
        """Seek so playback lines up with a stream moment captured at t0_perf."""
        self.ensure_playing()
        target = match_t + (time.perf_counter() - t0_perf) + offset
        self.seek(target + EMBED_SEEK_LATENCY)

    def toggle_pause(self):
        if not self.has_media:
            return
        state = self.mp.get_state()
        if state in (vlc.State.Ended, vlc.State.Stopped, vlc.State.NothingSpecial):
            self.mp.play()
        else:
            self.mp.pause()

    def pause(self):
        if self.mp.is_playing():
            self.mp.set_pause(1)

    def seek(self, seconds):
        self.mp.set_time(max(0, int(seconds * 1000)))

    def time(self):
        t = self.mp.get_time()
        return None if t is None or t < 0 else t / 1000.0

    def length(self):
        n = self.mp.get_length()
        return None if n is None or n <= 0 else n / 1000.0

    def nudge(self, delta):
        t = self.time()
        if t is not None:
            self.seek(t + delta)

    def set_mute(self, mute):
        self.mp.audio_set_mute(bool(mute))

    def set_fullscreen(self, flag):
        """Fullscreen for a libvlc-owned video window.

        Useless once a drawable is set (libvlc accepts the call, reports
        the new state, and changes nothing) - whoever owns the window has
        to resize it. mac_app does that on the Tk toplevel.
        """
        if self.nsview is not None:
            return False
        self.mp.set_fullscreen(bool(flag))
        return True

    def is_playing(self):
        return bool(self.mp.is_playing())

    def stop(self):
        self.mp.stop()

    # --- subtitles (embedded only; external mode uses VLC's own menu) ---

    def subtitle_tracks(self):
        try:
            descs = self.mp.video_get_spu_description() or []
        except Exception:
            return []
        out = []
        for tid, name in descs:
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            out.append((int(tid), str(name)))
        return out

    def set_subtitle(self, track_id):
        self.mp.video_set_spu(int(track_id))

    def add_subtitle_file(self, path):
        uri = Path(path).absolute().as_uri()
        self.mp.add_slave(vlc.MediaSlaveType.subtitle, uri, True)


class ExternalPlayer:
    """Controls a real VLC window via its built-in HTTP interface."""

    def __init__(self, exe=None, port=EXT_PORT, password=EXT_PASSWORD):
        self.exe = exe or self._find_vlc()
        self.port = port
        self.password = password
        self.proc = None
        self.has_media = False
        self._auth = "Basic " + base64.b64encode(
            (":" + password).encode()).decode()
        self._pulse_lock = threading.Lock()

    @staticmethod
    def _find_vlc():
        for p in VLC_EXE_CANDIDATES:
            if Path(p).is_file():
                return p
        raise VLCError("Could not find vlc.exe - install VLC from videolan.org.")

    def _request(self, params=None, timeout=2.0):
        url = f"http://127.0.0.1:{self.port}/requests/status.json"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": self._auth})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _cmd(self, command, val=None, key="val"):
        params = {"command": command}
        if val is not None:
            params[key] = val
        return self._request(params)

    def _alive(self):
        try:
            self._request(timeout=0.6)
            return True
        except Exception:
            return False

    def load(self, path):
        if self._alive():
            # in_play takes the MRL as `input=`; `val=` is silently ignored
            self._cmd("in_play", str(path), key="input")
        else:
            argv = [
                self.exe, "--extraintf", "http",
                "--http-host", "127.0.0.1",
                "--http-port", str(self.port),
                "--http-password", self.password,
                "--no-video-title-show",
            ]
            if sys.platform != "darwin":
                # Cocoa VLC has no one-instance option and treats unknown
                # options as fatal - it would exit before the HTTP interface
                # ever came up. (Irrelevant here anyway: we exec the binary
                # directly, so Launch Services never dedupes it.)
                argv.append("--no-one-instance")
            argv.append(str(path))
            self.proc = subprocess.Popen(argv)
            deadline = time.perf_counter() + 12.0
            while time.perf_counter() < deadline:
                if self._alive():
                    break
                time.sleep(0.25)
            else:
                raise VLCError("External VLC did not come up with its HTTP "
                               "interface enabled.")
        self.has_media = True

    def _status(self):
        try:
            return self._request(timeout=1.0)
        except Exception:
            return None

    def ensure_playing(self, timeout=8.0):
        if not self.has_media:
            raise VLCError("No video file loaded in external VLC.")
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            st = self._status()
            if st and st.get("state") == "playing":
                return
            if st and st.get("state") in ("paused", "stopped"):
                try:
                    self._cmd("pl_forceresume")
                    if st.get("state") == "stopped":
                        self._cmd("pl_play")
                except Exception:
                    pass
            time.sleep(0.2)
        raise VLCError("External VLC did not start playing in time.")

    def sync_seek(self, match_t, t0_perf, offset):
        """HTTP seeks are whole-second only: pick the next whole second the
        stream will reach and fire the seek at exactly that wall moment."""
        self.ensure_playing()

        def target():
            return match_t + (time.perf_counter() - t0_perf) + offset

        n = math.floor(target()) + 1
        if n - target() < 0.35:  # too tight to schedule reliably
            n += 1
        while True:
            d = (n - EXT_CMD_LEAD) - target()
            if d <= 0:
                break
            time.sleep(min(d, 0.05))
        self._cmd("seek", str(int(n)))

    def toggle_pause(self):
        try:
            self._cmd("pl_pause")
        except Exception:
            pass

    def pause(self):
        try:
            self._cmd("pl_forcepause")
        except Exception:
            pass

    def seek(self, seconds):
        try:
            self._cmd("seek", str(int(round(seconds))))
        except Exception:
            pass

    def time(self):
        st = self._status()
        if not st:
            return None
        length = st.get("length") or 0
        pos = st.get("position")
        if length > 0 and isinstance(pos, (int, float)) and pos > 0:
            return float(pos) * float(length)  # sub-second-ish precision
        t = st.get("time")
        return float(t) if isinstance(t, (int, float)) and t >= 0 else None

    def length(self):
        st = self._status()
        if st and (st.get("length") or 0) > 0:
            return float(st["length"])
        return None

    def nudge(self, delta):
        if abs(delta) < 1.0:
            threading.Thread(target=self._rate_pulse, args=(delta,),
                             daemon=True).start()
        else:
            t = self.time()
            if t is not None:
                self.seek(t + delta)

    def _rate_pulse(self, delta):
        """Shift playback by `delta` seconds smoothly: run at 1.5x (or 0.5x)
        until the drift is absorbed, then return to 1x."""
        if not self._pulse_lock.acquire(blocking=False):
            return  # a pulse is already running
        try:
            rate = 1.5 if delta > 0 else 0.5
            self._cmd("rate", str(rate))
            time.sleep(abs(delta) / abs(rate - 1.0))
            self._cmd("rate", "1.0")
        except Exception:
            pass
        finally:
            self._pulse_lock.release()

    def set_mute(self, mute):
        try:
            self._cmd("volume", "0" if mute else "256")
        except Exception:
            pass

    def is_playing(self):
        st = self._status()
        return bool(st and st.get("state") == "playing")

    def fullscreen_toggle(self):
        try:
            self._cmd("fullscreen")
        except Exception:
            pass

    def stop(self):
        # leave the user's VLC window alone on app close
        pass
