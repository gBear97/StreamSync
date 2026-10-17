"""Shared ffmpeg helpers for the audio matcher and the players.

StreamSync once also matched stream video frames against the file; that
method is gone, and what stays here is the part everything else leans on:
reading a file's duration and start time with the ffmpeg binary bundled
by imageio-ffmpeg, the error type the sync engine reports, and the flag
that keeps ffmpeg from flashing a console window on Windows.
"""

import re
import subprocess
import sys

import imageio_ffmpeg

_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_START_RE = re.compile(r"start:\s*(-?\d+(?:\.\d+)?)")


class MatchError(RuntimeError):
    pass


def _creationflags():
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def probe(path):
    """Return (duration_s, container_start_time_s)."""
    out = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", path],
        capture_output=True, text=True, errors="replace",
        creationflags=_creationflags(),
    ).stderr
    m = _DUR_RE.search(out)
    if not m:
        raise MatchError("ffmpeg could not read this file as a video.")
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = _START_RE.search(out)
    start = float(m.group(1)) if m else 0.0
    return duration, start
