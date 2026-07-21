"""Bring apps forward / hide them on macOS (AppleScript via osascript).

Used for the facecam swap: when the streamer pauses the film, the
browser app comes forward so the facecam is visible; when the film
resumes, the browser hides again. macOS prompts once for Automation
permission (System Events) the first time this runs.
"""

import os
import subprocess


def _osascript(script, timeout=6.0):
    r = subprocess.run(["osascript", "-e", script],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "osascript failed")
    return r.stdout.strip()


def _q(name):
    return name.replace("\\", "\\\\").replace('"', '\\"')


def list_gui_apps():
    """Names of running apps with a UI (candidates for the stream browser)."""
    out = _osascript(
        'tell application "System Events" to get name of every '
        "application process whose background only is false")
    return [n.strip() for n in out.split(",") if n.strip()]


def activate_app(name):
    """Unhide an app and bring it to the front."""
    _osascript(
        f'tell application "System Events" to tell application process '
        f'"{_q(name)}"\n'
        "set visible to true\n"
        "set frontmost to true\n"
        "end tell")


def hide_app(name):
    _osascript(
        f'tell application "System Events" to set visible of application '
        f'process "{_q(name)}" to false')


def activate_self():
    """Bring our own process (and its libvlc video window) to the front."""
    _osascript(
        'tell application "System Events" to set frontmost of '
        f"(first application process whose unix id is {os.getpid()}) to true")
