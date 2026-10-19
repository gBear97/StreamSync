"""In-app updates: ask GitHub for the latest release, then install it.

This is the one place StreamSync fetches and runs code it did not ship
with, so each step is checked rather than trusted:

- the release feed and the download are HTTPS to github.com, verified
  against real CA roots (see netcerts - a frozen build ships none of its
  own, so this was silently failing closed until it was fixed);
- the release's SHA256SUMS must carry a valid signature by the release
  key built into this app (SHA256SUMS.sig, see sigcheck), so the
  checksums themselves came from us - on Windows, which has no
  Authenticode signature, that is the whole proof of origin;
- the download must match its SHA256 in those checksums, which catches
  a truncated, corrupted or substituted file;
- on the Mac, the unpacked bundle must also carry an intact Developer ID
  signature from our own team;
- the new build is verified *before* anything touches the installed
  copy. On the Mac the bundle is swapped after the app has quit, by a
  helper that restores the old one if the move fails; on Windows the
  installer replaces the app's folder once the app has closed.

Stdlib only, so it stays importable from the dependency gate's context.
"""

import hashlib
import json
import os
import platform
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

import netcerts
import sigcheck
from version import __version__

REPO = "gBear97/StreamSync"
# Releases are signed with this Developer ID team. An update whose bundle
# is not signed by it is refused, so a tampered download cannot install
# itself even if it somehow matched the published checksum.
TEAM_ID = "52T7L6ZVY8"
# The public half of the release key that signs every release's
# SHA256SUMS (the private half is the repository secret
# RELEASE_SIGNING_KEY). Changing it means every installed app refuses
# every later release, so it changes only with a new key's rollout.
RELEASE_KEY = bytes.fromhex(
    "b696a60118d83a318b8754588ff19ff345a1b16caeaadefbc649ea43f576adb2")
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
SUMS_ASSET = "SHA256SUMS"
SIG_ASSET = "SHA256SUMS.sig"
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


def current_platform():
    """Which build of a release this process wants: "windows" or "mac"."""
    return "windows" if sys.platform == "win32" else "mac"


def pick_asset(assets, arch=None, plat=None):
    """This platform's build in a release: the Windows installer, or the
    .dmg for this Mac's architecture. Each test names its own suffix, so
    one platform can never pick up the other's build - the Windows name
    contains no -arm64- or -x86_64-, and only a Mac looks for .dmg."""
    if (plat or current_platform()) == "windows":
        for a in assets:
            name = a.get("name", "")
            if name.endswith("-setup.exe") and "-windows-x64-" in name:
                return a
        raise UpdateError("That release has no Windows installer.")
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


def install_dir(localappdata=None):
    """Where the Windows installer puts StreamSync (per user, no admin)."""
    local = localappdata or os.environ.get("LOCALAPPDATA") or ""
    return os.path.join(local, "Programs", "StreamSync") if local else None


def installed_exe_path(exe=None, frozen=None, localappdata=None):
    """Path of the installed StreamSync.exe we are running from, or None.

    Only a frozen build running from the installer's own folder updates
    itself. A source checkout is a developer's tree, and a copy unzipped
    from an older release lives wherever its owner put it, where a new
    installer would not land - both are sent to the Releases page instead.
    """
    if frozen is None:
        frozen = getattr(sys, "frozen", False)
    if not frozen:
        return None
    exe = os.path.abspath(exe or sys.executable)
    home = install_dir(localappdata)
    if not home or (os.path.normcase(os.path.dirname(exe))
                    != os.path.normcase(os.path.abspath(home))):
        return None
    return exe


# ------------------------------------------------------------------ network

def _get(url, timeout=30):
    with netcerts.urlopen(
            urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
        return r.read()


def _why(e):
    """A reason a user can act on, for whatever stopped the request."""
    reason = getattr(e, "reason", e)
    if isinstance(reason, ssl.SSLError):
        return ("Could not verify GitHub's certificate, so nothing was "
                f"downloaded. Update from {RELEASES_PAGE} instead.\n\n{reason}")
    return f"Could not reach GitHub: {e}"


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
        raise UpdateError(_why(e)) from e


def available_update():
    """(version, release) if a newer release exists, else (None, None)."""
    rel = fetch_latest()
    if not rel:
        return (None, None)
    tag = rel.get("tag_name") or rel.get("name") or ""
    return (tag, rel) if is_newer(tag) else (None, None)


def verified_sums(release, key=None):
    """The release's SHA256SUMS as text, once its signature checks out.

    The signature covers the exact bytes published, so the text is only
    decoded after it has been verified - never re-encoded and checked.
    """
    urls = {a.get("name"): a.get("browser_download_url")
            for a in release.get("assets", [])}
    if not urls.get(SUMS_ASSET) or not urls.get(SIG_ASSET):
        raise UpdateError(
            f"That release publishes no signed {SUMS_ASSET}, so it cannot "
            "be verified. Refusing to install it.")
    try:
        sums = _get(urls[SUMS_ASSET])
        sig = _get(urls[SIG_ASSET])
    except Exception as e:
        raise UpdateError(_why(e)) from e
    if not sigcheck.verify(key or RELEASE_KEY, sums, sig):
        raise UpdateError(
            f"That release's {SUMS_ASSET} is not signed by StreamSync's "
            "release key, so nothing in it can be trusted. Refusing to "
            "install it.")
    return sums.decode("utf-8", "replace")


def download_verified(asset, release, dest_dir, progress=None):
    """Download the asset and check it against the release's signed
    SHA256SUMS."""
    name = asset["name"]
    want = expected_sha256(verified_sums(release), name)
    if want is None:
        raise UpdateError(
            f"That release publishes no {SUMS_ASSET} entry for {name}, so the "
            "download cannot be verified. Refusing to install it.")

    path = os.path.join(dest_dir, name)
    digest = hashlib.sha256()
    total = asset.get("size") or 0
    done = 0
    req = urllib.request.Request(asset["browser_download_url"], headers=_UA)
    try:
        with netcerts.urlopen(req, timeout=60) as r, open(path, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except OSError as e:
        raise UpdateError(_why(e)) from e

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


def _tool_said(e):
    """The last line a failed tool printed, or how it failed to start."""
    if isinstance(e, subprocess.CalledProcessError):
        lines = (e.stderr or e.stdout or "").strip().splitlines()
        if lines:
            return lines[-1]
        return f"{e.cmd[0]} exited with status {e.returncode}"
    return str(e)


def _attach(dmg_path, mount):
    """Mount the disk image read-only at `mount`, hidden from Finder.

    hdiutil first, then `diskutil image attach`. macOS 27 marks the
    hdiutil attach and detach forms used here as deprecated in favour of
    diskutil image, which older macOS does not have. If Apple removes
    them, every Mac update would otherwise stop at this step - and an app
    that cannot update cannot be updated out of it.
    """
    try:
        subprocess.run(["hdiutil", "attach", dmg_path, "-mountpoint", mount,
                        "-nobrowse", "-readonly"],
                       check=True, capture_output=True, text=True)
        return
    except (OSError, subprocess.CalledProcessError) as e:
        first = e
    try:
        subprocess.run(["diskutil", "image", "attach", "--readOnly",
                        "--mountOptions", "nobrowse", "--mountPoint", mount,
                        dmg_path],
                       check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as e:
        # hdiutil's reason, unless hdiutil is not there at all
        why = _tool_said(e if isinstance(first, OSError) else first)
        raise UpdateError(
            f"The disk image could not be opened.\n\n{why}") from e


def _detach(mount):
    """Unmount the disk image, by whichever tool still works, and remove
    the mount folder - only once it is empty, so never a mounted one."""
    for cmd in (["hdiutil", "detach", mount], ["diskutil", "eject", mount]):
        try:
            if subprocess.run(cmd, capture_output=True,
                              text=True).returncode == 0:
                break
        except OSError:
            pass
    try:
        os.rmdir(mount)
    except OSError:
        pass


def stage(dmg_path, app_path, log=print):
    """Copy the new bundle out of the disk image, beside the installed one.

    Nothing here touches the running app: on any failure the installed
    copy is exactly as it was.
    """
    mount = tempfile.mkdtemp(prefix="streamsync-update-")
    log("Mounting the disk image...")
    try:
        _attach(dmg_path, mount)
    except UpdateError:
        _detach(mount)       # in case it half-attached; removes the folder
        raise
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
        _detach(mount)

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


# The installer's own switches: no wizard and no prompts, no reboot, and
# /relaunch=1 tells installer/StreamSync.iss to start the new version.
SETUP_ARGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/relaunch=1"]


def install_and_relaunch_windows(setup_path):
    """Start the verified installer, detached, and tell the caller to quit.

    The installer writes into the same per-user folder, closing this app
    first if it is still open (CloseApplications=force), and starts the
    new version when it is done. The caller quits through its normal
    close straight after, so its settings are saved before the files are
    replaced. A file the app downloaded itself carries no mark of the web,
    so SmartScreen does not stop the installer the way it may stop a
    first install from the browser.
    """
    detached = 0x00000008 | 0x00000200   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    subprocess.Popen([setup_path] + SETUP_ARGS, creationflags=detached,
                     close_fds=True, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)
