"""Hosting a watch party from this computer, through a free Cloudflare
quick tunnel.

The relay runs inside StreamSync, reachable only from this computer
(127.0.0.1): no Python to install, no firewall question, nothing open to
the network. cloudflared makes it reachable from anywhere, at a new
https://….trycloudflare.com address, for as long as the party lasts. It
is used where it is installed (on PATH, Homebrew's, the MSI's); failing
that, StreamSync downloads Cloudflare's own build from its releases on
GitHub into StreamSync's own folder, and runs it only once the published
checksum and Cloudflare's signature check out.

A quick tunnel holds 200 connections at once (measured 2026-10-22). The
host's own session connects to the relay directly, not through the
tunnel, so all 200 are viewers'.

Nothing here touches Tk. The Watch party window drives Hosting.start from
a worker thread, and Leave and quit stop it.
"""

import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from collections import deque

from websockets.asyncio.server import serve

import netcerts
import relay_server
from version import __version__

RELEASES = "https://api.github.com/repos/cloudflare/cloudflared/releases/latest"
DOH = "https://cloudflare-dns.com/dns-query?name={}&type=A"
DOWNLOAD_PAGE = ("https://developers.cloudflare.com/cloudflare-one/networks/"
                 "connectors/cloudflare-tunnel/downloads/")
MAC_TEAM = "68WVV388M8"            # Developer ID Application: Cloudflare Inc.
WIN_SIGNER = 'CN="Cloudflare, Inc."'
EXE = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
APPROX_MB = 52 if sys.platform == "win32" else 20
VIEWERS = 200                      # a quick tunnel's connections, all viewers'
TUNNEL_TIMEOUT = 45.0
STALE_DAYS = 270                   # an own copy this old is fetched afresh
OWN_ONLY_ENV = "STREAMSYNC_OWN_CLOUDFLARED"   # "1": ignore installed copies

_UA = {"User-Agent": f"StreamSync/{__version__}"}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_CODE = re.compile(r"\b([A-Z2-9]{4}-[A-Z2-9]{4})\b", re.I)
_ADDR = re.compile(r"\b(?:wss?|https?)://[^\s<>\"']+", re.I)


class DownloadError(RuntimeError):
    """Why cloudflared could not be fetched, in words for the user."""


class Cancelled(RuntimeError):
    """Hosting was stopped while it was still starting."""


# --------------------------------------------------------------- finding

def own_dir():
    """StreamSync's own folder for cloudflared: outside the app's install
    folder, so an update of StreamSync leaves it alone."""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/StreamSync")
    elif sys.platform == "win32":
        base = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                            "StreamSync")
    else:
        base = os.path.join(os.environ.get("XDG_DATA_HOME")
                            or os.path.expanduser("~/.local/share"), "StreamSync")
    return os.path.join(base, "cloudflared")


def system_paths():
    """Where a cloudflared installed by hand lives: on PATH, Homebrew's two
    prefixes (an app opened from the Finder has neither on its PATH), and
    the Windows installer's Program Files folders."""
    paths = [shutil.which("cloudflared")]
    if sys.platform == "darwin":
        paths += ["/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared"]
    elif sys.platform == "win32":
        for var in ("ProgramFiles(x86)", "ProgramFiles"):
            root = os.environ.get(var)
            if root:
                paths.append(os.path.join(root, "cloudflared", "cloudflared.exe"))
    out = []
    for p in paths:
        if p and p not in out:
            out.append(p)
    return out


def version_of(path, timeout=20):
    """The version of the cloudflared at `path` ("2026.10.3"), or None if
    it does not run."""
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True,
                           timeout=timeout, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"cloudflared version (\d+\.\d+\.\d+)", r.stdout + r.stderr)
    return m.group(1) if m else None


def stale(version, today=None):
    """True for a version (cloudflared's are dated, "2026.10.3") older than
    STALE_DAYS: Cloudflare supports a release for about a year."""
    m = re.match(r"(\d{4})\.(\d{1,2})\.", version or "")
    if not m:
        return True
    t = today or time.localtime()
    months = (t.tm_year - int(m.group(1))) * 12 + (t.tm_mon - int(m.group(2)))
    return months * 30 > STALE_DAYS


def find(own_only=None):
    """(path, version, own) of a cloudflared that runs, else (None, None,
    False). One installed by hand comes first - its owner keeps it up to
    date - then StreamSync's own copy, unless that has gone stale."""
    if own_only is None:
        own_only = os.environ.get(OWN_ONLY_ENV) == "1"
    if not own_only:
        for path in system_paths():
            if os.path.isfile(path):
                v = version_of(path)
                if v:
                    return path, v, False
    path = os.path.join(own_dir(), EXE)
    if os.path.isfile(path):
        v = version_of(path)
        if v and not stale(v):
            return path, v, True
    return None, None, False


# ------------------------------------------------------------ downloading

def asset_name(plat=None, machine=None):
    """The release file for this computer, or None if Cloudflare has none."""
    plat = plat or sys.platform
    machine = (machine or platform.machine()).lower()
    if plat == "win32":
        # ARM64 Windows runs the x64 build
        return ("cloudflared-windows-386.exe" if machine in ("x86", "i386", "i686")
                else "cloudflared-windows-amd64.exe")
    if plat == "darwin":
        return ("cloudflared-darwin-arm64.tgz" if machine == "arm64"
                else "cloudflared-darwin-amd64.tgz")
    if plat.startswith("linux"):
        return {"x86_64": "cloudflared-linux-amd64", "amd64": "cloudflared-linux-amd64",
                "aarch64": "cloudflared-linux-arm64",
                "arm64": "cloudflared-linux-arm64"}.get(machine)
    return None


def _short(e):
    return str(getattr(e, "reason", None) or e) or type(e).__name__


def latest_release():
    try:
        with netcerts.urlopen(urllib.request.Request(RELEASES, headers=_UA),
                              timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        raise DownloadError(f"could not read Cloudflare's releases on GitHub ({_short(e)})")


def download(progress=None, dest=None, release=None, check_signature=None):
    """Fetch the latest cloudflared into `dest` (StreamSync's own folder),
    checked against the SHA-256 the release publishes and for Cloudflare's
    signature before it is first run. Returns (path, version); raises
    DownloadError, leaving nothing behind but a copy that was already there.
    progress(done_bytes, total_bytes) runs as it downloads."""
    dest = dest or own_dir()
    name = asset_name()
    if name is None:
        raise DownloadError("Cloudflare makes no cloudflared for this computer")
    rel = release if release is not None else latest_release()
    asset = next((a for a in rel.get("assets", ()) if a.get("name") == name), None)
    if asset is None:
        raise DownloadError(f"Cloudflare's latest release ({rel.get('tag_name', '?')}) "
                            f"has no {name}")
    digest = str(asset.get("digest") or "")
    if not digest.startswith("sha256:"):
        raise DownloadError("Cloudflare's release lists no checksum for it, so it was "
                            "not downloaded")
    want = digest[len("sha256:"):].lower()
    os.makedirs(dest, exist_ok=True)
    work = tempfile.mkdtemp(prefix="download-", dir=dest)
    try:
        got = os.path.join(work, name)
        h, done = hashlib.sha256(), 0
        try:
            req = urllib.request.Request(asset["browser_download_url"], headers=_UA)
            with netcerts.urlopen(req, timeout=60) as r, open(got, "wb") as f:
                total = int(r.headers.get("Content-Length") or asset.get("size") or 0)
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
        except OSError as e:
            raise DownloadError(f"the download failed ({_short(e)})")
        if h.hexdigest() != want:
            raise DownloadError("the download did not match Cloudflare's published "
                                "checksum, so it was thrown away")
        exe = os.path.join(work, EXE)
        if name.endswith(".tgz"):
            _extract(got, exe)
        else:
            os.replace(got, exe)
        if sys.platform != "win32":
            os.chmod(exe, 0o755)
        (check_signature or verify_signature)(exe)
        version = version_of(exe)
        if version is None:
            raise DownloadError("the downloaded cloudflared does not run")
        final = os.path.join(dest, EXE)
        os.replace(exe, final)
        return final, version
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _extract(tgz, out):
    """The cloudflared a release archive holds, written to `out` and only
    there: member paths are never used, so none can point elsewhere."""
    try:
        with tarfile.open(tgz, "r:gz") as t:
            member = next((m for m in t.getmembers() if m.isfile()
                           and os.path.basename(m.name) == "cloudflared"), None)
            if member is None or member.size > 300 * 1024 * 1024:
                raise DownloadError("the archive holds no cloudflared")
            with t.extractfile(member) as src, open(out, "wb") as f:
                shutil.copyfileobj(src, f)
    except (tarfile.TarError, OSError) as e:
        raise DownloadError(f"the archive could not be read ({_short(e)})")


def verify_signature(path):
    """Cloudflare's own signature on `path`, or DownloadError. The checksum
    says the download is the file the release lists; this says Cloudflare
    made it. (No other platform signs its builds: Linux is checked by
    checksum alone.)"""
    if sys.platform == "darwin":
        req = f'anchor apple generic and certificate leaf[subject.OU] = "{MAC_TEAM}"'
        try:
            r = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", f"-R={req}", path],
                               capture_output=True, text=True, timeout=60)
            ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
    elif sys.platform == "win32":
        ps = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32",
                          "WindowsPowerShell", "v1.0", "powershell.exe")
        script = ("$s = Get-AuthenticodeSignature -LiteralPath $env:SS_SIGNED; "
                  "[string]$s.Status + '|' + $s.SignerCertificate.Subject")
        try:
            r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, text=True, timeout=60,
                               env=dict(os.environ, SS_SIGNED=path),
                               creationflags=_NO_WINDOW)
            status, _, subject = r.stdout.strip().partition("|")
            ok = status == "Valid" and subject.startswith(WIN_SIGNER)
        except (OSError, subprocess.SubprocessError):
            ok = False
    else:
        return
    if not ok:
        raise DownloadError("it is not signed by Cloudflare, so it was thrown away")


# ---------------------------------------------------------------- running

class LocalRelay:
    """relay_server inside StreamSync, listening on 127.0.0.1 only, in a
    thread with an event loop of its own. start() returns its port."""

    def __init__(self):
        self.port = None
        self.address = None          # (host, port) it listens on
        self._loop = self._stopped = self._thread = None

    def start(self, timeout=10.0):
        ready, failed = threading.Event(), []

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)     # this thread's, for the relay's own calls
            self._loop = loop
            try:
                loop.run_until_complete(self._serve(ready))
            except Exception as e:
                failed.append(e)
            finally:
                ready.set()
                # a room's expiry timer and the like: cancelled, not left
                # pending in a loop that is going away
                left = asyncio.all_tasks(loop)
                for task in left:
                    task.cancel()
                if left:
                    loop.run_until_complete(asyncio.gather(*left, return_exceptions=True))
                loop.close()

        self._thread = threading.Thread(target=run, daemon=True, name="relay")
        self._thread.start()
        if not ready.wait(timeout) or failed or self.port is None:
            self.stop()
            why = _short(failed[0]) if failed else "it timed out"
            raise RuntimeError(f"the relay did not start ({why})")
        return self.port

    async def _serve(self, ready):
        self._stopped = asyncio.Event()

        async def handler(ws):
            await relay_server.handle(ws)

        async with serve(handler, "127.0.0.1", 0, max_size=relay_server.MAX_MSG) as server:
            self.address = next(iter(server.sockets)).getsockname()[:2]
            self.port = self.address[1]
            ready.set()
            await self._stopped.wait()
        relay_server.rooms.clear()

    def stop(self):
        loop, stopped = self._loop, self._stopped
        if loop is not None and stopped is not None:
            try:
                loop.call_soon_threadsafe(stopped.set)
            except RuntimeError:
                pass                 # its loop has ended already
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(5)


class QuickTunnel:
    """cloudflared's quick tunnel to a port on this computer. start() waits
    for its address and its first registered connection to Cloudflare -
    the address is printed a moment before anyone can use it."""

    def __init__(self, exe, port):
        self.exe, self.port = exe, port
        self.url = None
        self.proc = None
        self.lines = deque(maxlen=40)
        self.on_exit = None          # it ended by itself (called from its reader)
        self._registered = threading.Event()
        self._ended = threading.Event()
        self._stopping = False

    def start(self, timeout=TUNNEL_TIMEOUT):
        # exe: cloudflared's path - or a command as a list (the tests' stand-in)
        cmd = list(self.exe) if isinstance(self.exe, (list, tuple)) else [self.exe]
        self.proc = subprocess.Popen(
            cmd + ["tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
        if self._stopping:           # stopped while it was being started
            self.stop()
            raise Cancelled()
        threading.Thread(target=self._read, daemon=True, name="cloudflared").start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._registered.wait(0.2) and self.url:
                return self.url
            if self._ended.is_set():
                break
        why = self._why(timeout)
        self.stop()
        raise RuntimeError(why)

    def _read(self):
        for line in self.proc.stdout:
            line = line.rstrip()
            self.lines.append(line)
            if self.url is None:
                m = _URL.search(line)
                if m:
                    self.url = m.group(0)
            if "Registered tunnel connection" in line:
                self._registered.set()
        self.proc.wait()
        self._ended.set()
        if not self._stopping and self.on_exit is not None:
            self.on_exit()

    def _why(self, timeout):
        if self._ended.is_set():
            errors = [l for l in self.lines if " ERR " in l]
            detail = (errors[-1].split(" ERR ", 1)[1].strip() if errors
                      else f"it exited with code {self.proc.returncode}")
            return f"cloudflared stopped before the tunnel opened: {detail}"
        return (f"the tunnel did not open within {timeout:.0f} s - this network may "
                "be blocking cloudflared")

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        self._stopping = True
        p = self.proc
        if p is None or p.poll() is not None:
            return
        p.terminate()
        try:
            p.wait(3)                # it may wait on open connections to close
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(5)


def wait_for_name(name, timeout=20.0, cancelled=lambda: False):
    """True once Cloudflare's DNS has a new tunnel's `name`, else False
    after `timeout`. For a second or so after the tunnel opens, the name
    does not resolve; a viewer who looked it up then would be told "no such
    name" for as long as their resolver keeps that answer (it failed a
    join for 40 s in testing). Asked over HTTPS, so no resolver on this
    computer keeps the early answer either - not that the host needs the
    name: its own session talks to the relay directly."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not cancelled():
        try:
            req = urllib.request.Request(
                DOH.format(name), headers=dict(_UA, accept="application/dns-json"))
            with netcerts.urlopen(req, timeout=5) as r:
                answer = json.loads(r.read())
            if answer.get("Status") == 0 and answer.get("Answer"):
                return True
        except Exception:
            pass                     # DNS over HTTPS blocked here: the time limit decides
        time.sleep(0.5)
    return False


class Hosting:
    """A party hosted from this computer: the relay inside StreamSync and
    a quick tunnel to it. start() runs on a worker thread and returns the
    address to share; stop(), from any thread, also cancels a start that
    is still under way."""

    def __init__(self):
        self.relay = LocalRelay()
        self.tunnel = None
        self.local_url = self.public_url = None
        self.dns_live, self.dns_seconds = None, None   # how the address went live
        self.on_down = None          # the tunnel ended by itself, mid-party
        self._cancelled = threading.Event()

    @property
    def cancelled(self):
        return self._cancelled.is_set()

    def start(self, exe, say=lambda text: None):
        self._check()                    # stopped before it began: start nothing
        say("Starting the relay on this computer...")
        port = self.relay.start()
        self.local_url = f"ws://127.0.0.1:{port}"
        self._check()
        say("Opening a free Cloudflare tunnel - a few seconds...")
        self.tunnel = QuickTunnel(exe, port)
        self.tunnel.on_exit = self._down
        if self.cancelled:
            raise Cancelled()
        try:
            url = self.tunnel.start()
        except RuntimeError:
            self._check()            # stopped meanwhile: say so, not why it broke
            raise
        self._check()
        say("Waiting for the address to go live...")
        t0 = time.monotonic()
        self.dns_live = wait_for_name(url[len("https://"):], cancelled=lambda: self.cancelled)
        self.dns_seconds = time.monotonic() - t0
        self._check()
        self.public_url = "wss://" + url[len("https://"):]
        return self.public_url

    def _check(self):
        if self.cancelled:
            raise Cancelled()

    def _down(self):
        if not self.cancelled and self.on_down is not None:
            self.on_down()

    def alive(self):
        return self.tunnel is not None and self.tunnel.alive()

    def stop(self):
        self._cancelled.set()
        if self.tunnel is not None:
            self.tunnel.stop()
        self.relay.stop()

    def kill(self):
        """At quit: cloudflared ended now if stop() has not ended it yet. A
        process of its own, it would otherwise outlive StreamSync."""
        self._cancelled.set()
        t = self.tunnel
        if t is not None:
            t._stopping = True
            if t.alive():
                try:
                    t.proc.kill()
                except OSError:
                    pass


# ---------------------------------------------------------------- invites

def invite(address, code):
    """One line to send viewers: the relay's address and the code."""
    return f"StreamSync watch party: {address} code {code}"


def parse_invite(text):
    """(relay address or None, code or None) from a pasted invite - or from
    a code alone. The code is looked for outside the address: a tunnel's
    words can look like one."""
    text = text or ""
    addr = _ADDR.search(text)
    code = _CODE.search(_ADDR.sub(" ", text))
    return (addr.group(0).rstrip(".,;:)") if addr else None,
            code.group(1).upper() if code else None)
