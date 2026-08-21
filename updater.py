"""In-app updates: ask GitHub for the latest release, then install it.

This is the one place StreamSync fetches and runs code it did not ship
with, so each step is checked rather than trusted:

- the release feed and the download are HTTPS to github.com;
- the disk image must match the SHA256 published beside it in the same
  release, which catches a truncated or corrupted download;
- the unpacked bundle must carry an intact Developer ID signature from
  our own team, which is what actually establishes the update came from
  us rather than merely arriving intact;
- the new bundle is copied out of the image and verified *before*
  anything touches the installed copy, and the swap itself happens after
  the app has quit, from a helper that restores the old bundle if the
  move fails.

Stdlib only, so it stays importable from the dependency gate's context.
"""

import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

from version import __version__

REPO = "gBear97/StreamSync"
# Releases are signed with this Developer ID team. An update whose bundle
# is not signed by it is refused, so a tampered download cannot install
# itself even if it somehow matched the published checksum.
TEAM_ID = "52T7L6ZVY8"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
SUMS_ASSET = "SHA256SUMS"
_UA = {"User-Agent": "StreamSync-updater", "Accept": "application/vnd.github+json"}


class UpdateError(Exception):
    """Anything that should stop an update, with a user-readable reason."""


# ------------------------------------------------------------ pure helpers

def parse_version(text):
    """'v1.2.3' -> (1, 2, 3). Missing or junk components sort as 0."""
    parts = []
    for chunk in str(text).strip().lstrip("vV").split(".")[:3]:
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts + [0] * (3 - len(parts)))


def is_newer(candidate, current=__version__):
    return parse_version(candidate) > parse_version(current)


def current_arch():
    """The architecture this process is running as, as CI names it."""
    return "arm64" if platform.machine() == "arm64" else "x86_64"


def pick_asset(assets, arch=None):
    """The release's .dmg for this architecture."""
    arch = arch or current_arch()
    for a in assets:
        name = a.get("name", "")
        if name.endswith(".dmg") and f"-{arch}-" in name:
            return a
    raise UpdateError(f"That release has no disk image for {arch}.")


def expected_sha256(sums_text, filename):
    """Look up one file's digest in a sha256sum-style listing."""
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and os.path.basename(parts[-1]) == filename:
            return parts[0].lower()
    return None


def installed_app_path():
    """Path of the .app we are running from, or None when run from source.

    Running from a checkout must never self-replace: there is no bundle to
    swap, and a developer's working tree is not ours to overwrite.
    """
    exe = os.path.abspath(sys.executable)
    marker = ".app/Contents/MacOS/"
    i = exe.find(marker)
    return exe[:i + len(".app")] if i != -1 else None


# ------------------------------------------------------------------ network

def _get(url, timeout=30):
    with urllib.request.urlopen(
            urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
        return r.read()


def fetch_latest(timeout=15):
    """The latest published release, or None if the repo has none yet.

    A repo with no releases answers 404, which is not a failure - it is
    just nothing to offer, and must not be reported as unreachable.
    """
    try:
        return json.loads(_get(LATEST_URL, timeout))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise UpdateError(f"GitHub returned HTTP {e.code}.") from e
    except Exception as e:
        raise UpdateError(f"Could not reach GitHub: {e}") from e


def available_update():
    """(version, release) if a newer release exists, else (None, None)."""
    rel = fetch_latest()
    if not rel:
        return (None, None)
    tag = rel.get("tag_name") or rel.get("name") or ""
    return (tag, rel) if is_newer(tag) else (None, None)


def download_verified(asset, sums_url, dest_dir, progress=None):
    """Download the asset and check it against the release's SHA256SUMS."""
    name = asset["name"]
    want = None
    if sums_url:
        try:
            want = expected_sha256(_get(sums_url).decode("utf-8", "replace"), name)
        except Exception:
            want = None
    if want is None:
        raise UpdateError(
            f"That release publishes no {SUMS_ASSET} entry for {name}, so the "
            "download cannot be verified. Refusing to install it.")

    path = os.path.join(dest_dir, name)
    digest = hashlib.sha256()
    total = asset.get("size") or 0
    done = 0
    req = urllib.request.Request(asset["browser_download_url"], headers=_UA)
    with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)

    got = digest.hexdigest()
    if got != want:
        os.remove(path)
        raise UpdateError(
            f"Checksum mismatch for {name} - expected {want[:12]}..., "
            f"got {got[:12]}.... The download was not installed.")
    return path


# ------------------------------------------------------------------ install

# Runs after we exit, so nothing swaps a bundle that is still open. Keeps
# the old app until the new one is in place, and puts it back if not.
_SWAP = r"""#!/bin/sh
PID="$1"; APP="$2"; NEW="$3"; OLD="$APP.old"
i=0
while kill -0 "$PID" 2>/dev/null && [ $i -lt 150 ]; do
    sleep 0.2
    i=$((i + 1))
done
rm -rf "$OLD"
mv "$APP" "$OLD" || exit 1
if mv "$NEW" "$APP"; then
    rm -rf "$OLD"
else
    mv "$OLD" "$APP"
    exit 1
fi
open -a "$APP"
"""


def signing_team(app_path):
    """The Team ID an .app is signed by, or None if it is unsigned."""
    try:
        r = subprocess.run(["codesign", "-dv", "--verbose=4", app_path],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    # codesign writes its report to stderr.
    for line in (r.stderr or "").splitlines():
        if line.startswith("TeamIdentifier="):
            team = line.split("=", 1)[1].strip()
            return None if team in ("", "not set") else team
    return None


def verify_signature(app_path, team_id=TEAM_ID):
    """Refuse anything not intact and signed by our own Developer ID.

    Replaces stripping the quarantine flag, which is what this used to do:
    that told macOS to stop checking, where this checks properly. The
    quarantine flag can stay on the bundle - a valid Developer ID
    signature satisfies Gatekeeper on its own.
    """
    try:
        r = subprocess.run(
            ["codesign", "--verify", "--strict", "--deep", "--verbose=2",
             app_path],
            capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as e:
        raise UpdateError(f"Could not run codesign: {e}") from e
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip().splitlines()
        raise UpdateError(
            "The downloaded app's signature is not intact, so it was not "
            "installed." + (f"\n\n{detail[-1]}" if detail else ""))

    team = signing_team(app_path)
    if team != team_id:
        raise UpdateError(
            f"The downloaded app is signed by {team or 'nobody'}, not by "
            f"{team_id}. Refusing to install it.")
    return True


def stage(dmg_path, app_path, log=print):
    """Copy the new bundle out of the disk image, beside the installed one.

    Nothing here touches the running app: on any failure the installed
    copy is exactly as it was.
    """
    mount = tempfile.mkdtemp(prefix="streamsync-update-")
    log("Mounting the disk image...")
    subprocess.run(["hdiutil", "attach", dmg_path, "-mountpoint", mount,
                    "-nobrowse", "-readonly"],
                   check=True, capture_output=True, text=True)
    staged = app_path + ".new"
    try:
        src = os.path.join(mount, "StreamSync.app")
        if not os.path.isdir(src):
            raise UpdateError("The disk image does not contain StreamSync.app.")
        subprocess.run(["rm", "-rf", staged], check=False)
        log("Copying the new version into place...")
        subprocess.run(["ditto", src, staged], check=True,
                       capture_output=True, text=True)
    finally:
        subprocess.run(["hdiutil", "detach", mount], check=False,
                       capture_output=True, text=True)

    if not os.path.isdir(staged):
        subprocess.run(["rm", "-rf", staged], check=False)
        raise UpdateError("The new version could not be copied out.")

    log("Checking the signature...")
    try:
        verify_signature(staged)
    except UpdateError:
        # Never leave an unverified bundle sitting next to the real one.
        subprocess.run(["rm", "-rf", staged], check=False)
        raise
    return staged


def swap_and_relaunch(app_path, staged):
    """Hand the swap to a detached helper and tell the caller to quit."""
    fd, script = tempfile.mkstemp(prefix="streamsync-swap-", suffix=".sh")
    with os.fdopen(fd, "w") as f:
        f.write(_SWAP)
    os.chmod(script, 0o755)
    subprocess.Popen(["/bin/sh", script, str(os.getpid()), app_path, staged],
                     start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
