"""Hosting through this computer (hosting.py): finding cloudflared,
downloading it safely, the relay inside StreamSync, the quick tunnel, the
controller's part, and invites. No network: a local web server stands in
for GitHub, a Python script for cloudflared, and DNS over HTTPS is asked
of a stub.

Run: python test_tunnel.py
"""

import hashlib
import io
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import controller
import hosting
import relay_server
import session

TMP = tempfile.mkdtemp(prefix="test_tunnel-")

FAKE = r'''
import os, sys, time
mode = os.environ.get("FAKE_MODE", "ok")
def say(s):
    print(s, flush=True)
if mode == "fail":
    say("2026-10-22T08:15:20Z INF Requesting new quick Tunnel on trycloudflare.com...")
    say("2026-10-22T08:15:21Z ERR failed to request quick Tunnel: 429 Too Many Requests")
    sys.exit(1)
if mode == "silent":
    time.sleep(60)
say("2026-10-22T08:15:20Z INF Requesting new quick Tunnel on trycloudflare.com...")
say("2026-10-22T08:15:22Z INF +----------------------------------------------------+")
say("2026-10-22T08:15:22Z INF |  Your quick Tunnel has been created! Visit it at   |")
say("2026-10-22T08:15:22Z INF |  https://fake-able-mind-words.trycloudflare.com    |")
say("2026-10-22T08:15:22Z INF +----------------------------------------------------+")
if mode == "slow":
    time.sleep(3)
say("2026-10-22T08:15:23Z INF Registered tunnel connection connIndex=0 location=lax05 protocol=quic")
if mode == "die":
    time.sleep(0.5)
    sys.exit(0)
time.sleep(60)
'''
FAKE_PATH = os.path.join(TMP, "fake_cloudflared.py")
with open(FAKE_PATH, "w") as f:
    f.write(FAKE)
FAKE_EXE = [sys.executable, "-u", FAKE_PATH]
FAKE_WSS = "wss://fake-able-mind-words.trycloudflare.com"


def mode(m):
    os.environ["FAKE_MODE"] = m


def wait(cond, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def refused(port):
    s = socket.socket()
    s.settimeout(1)
    try:
        return s.connect_ex(("127.0.0.1", port)) != 0
    finally:
        s.close()


def room_works(url):
    """A host opens a room at `url` and a viewer joins it."""
    stop = threading.Event()
    host, created = session._connect(url, queue.Queue(), {"type": "create", "meta": {}},
                                     ("created",), stop, time.monotonic() + 5)
    viewer, joined = session._connect(url, queue.Queue(),
                                      {"type": "join", "code": created["code"]},
                                      ("joined",), stop, time.monotonic() + 5)
    host.close()
    viewer.close()
    return joined["type"] == "joined"


# ------------------------------------------------------------------ pieces

def names_and_ages():
    a = hosting.asset_name
    assert a("darwin", "arm64") == "cloudflared-darwin-arm64.tgz"
    assert a("darwin", "x86_64") == "cloudflared-darwin-amd64.tgz"
    assert a("win32", "AMD64") == "cloudflared-windows-amd64.exe"
    assert a("win32", "ARM64") == "cloudflared-windows-amd64.exe", "ARM64 runs the x64 build"
    assert a("win32", "x86") == "cloudflared-windows-386.exe"
    assert a("linux", "x86_64") == "cloudflared-linux-amd64"
    assert a("linux", "riscv64") is None
    june = time.strptime("2026-06", "%Y-%m")
    assert not hosting.stale("2026.1.0", june), "five months is fine"
    assert hosting.stale("2025.8.0", june), "ten months is stale"
    assert hosting.stale("garbage", june)
    print("asset names for each platform; an own copy goes stale after ~9 months")


def finding():
    real = hosting.system_paths, hosting.own_dir, hosting.version_of
    own = os.path.join(TMP, "own")
    system = os.path.join(TMP, "system", hosting.EXE)
    os.makedirs(own)
    os.makedirs(os.path.dirname(system))
    for p in (system, os.path.join(own, hosting.EXE)):
        open(p, "w").close()
    versions = {system: "2026.9.1", os.path.join(own, hosting.EXE): "2026.10.3"}
    hosting.system_paths = lambda: [os.path.join(TMP, "missing", hosting.EXE), system]
    hosting.own_dir = lambda: own
    hosting.version_of = lambda p, timeout=20: versions.get(p)
    try:
        now = time.localtime()
        versions[os.path.join(own, hosting.EXE)] = f"{now.tm_year}.{now.tm_mon}.0"
        assert hosting.find(own_only=False) == (system, "2026.9.1", False), \
            "one installed by hand comes first"
        assert hosting.find(own_only=True)[2] is True, "own_only skips installed copies"
        os.environ[hosting.OWN_ONLY_ENV] = "1"
        assert hosting.find()[2] is True, "and so does the environment switch"
        del os.environ[hosting.OWN_ONLY_ENV]
        versions[os.path.join(own, hosting.EXE)] = "2024.1.0"
        assert hosting.find(own_only=True) == (None, None, False), "a stale own copy is none"
        versions[system] = None                     # installed, but does not run
        assert hosting.find(own_only=False) == (None, None, False)
    finally:
        hosting.system_paths, hosting.own_dir, hosting.version_of = real
    print("find: an installed copy first, then StreamSync's own unless stale")


class Serve(BaseHTTPRequestHandler):
    body = b""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


def fake_asset(binary):
    """This platform's release file around `binary`."""
    name = hosting.asset_name()
    if name.endswith(".tgz"):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            info = tarfile.TarInfo("cloudflared")
            info.size = len(binary)
            t.addfile(info, io.BytesIO(binary))
        return name, buf.getvalue()
    return name, binary


def downloading():
    web = ThreadingHTTPServer(("127.0.0.1", 0), Serve)
    threading.Thread(target=web.serve_forever, daemon=True).start()
    real_version = hosting.version_of
    hosting.version_of = lambda p, timeout=20: "2026.10.3"
    try:
        binary = b"pretend this is cloudflared"
        name, Serve.body = fake_asset(binary)
        good = "sha256:" + hashlib.sha256(Serve.body).hexdigest()

        def release(digest=good, asset_name=name):
            return {"tag_name": "2026.10.3", "assets": [{
                "name": asset_name, "digest": digest, "size": len(Serve.body),
                "browser_download_url": f"http://127.0.0.1:{web.server_port}/asset"}]}

        accept = lambda path: None             # noqa: E731
        dest = os.path.join(TMP, "dl")
        seen = []
        path, version = hosting.download(progress=lambda d, t: seen.append((d, t)), dest=dest,
                                         release=release(), check_signature=accept)
        assert path == os.path.join(dest, hosting.EXE) and version == "2026.10.3"
        assert open(path, "rb").read() == binary
        assert os.listdir(dest) == [hosting.EXE], os.listdir(dest)
        assert seen and seen[-1] == (len(Serve.body), len(Serve.body)), seen

        # a failed download leaves the copy that was there alone
        open(path, "wb").write(b"old copy")

        def fails(want, **kw):
            try:
                hosting.download(dest=dest, check_signature=kw.pop("check", accept), **kw)
            except hosting.DownloadError as e:
                assert want in str(e), (want, str(e))
            else:
                raise AssertionError(f"downloaded, though: {want}")
            assert os.listdir(dest) == [hosting.EXE], os.listdir(dest)
            assert open(path, "rb").read() == b"old copy"

        fails("did not match", release=release(digest="sha256:" + "0" * 64))
        fails("no checksum", release=release(digest=""))
        fails(f"has no {name}", release=release(asset_name="cloudflared-amiga.lha"))

        def refuse(p):
            raise hosting.DownloadError("it is not signed by Cloudflare, so it was thrown away")
        fails("not signed by Cloudflare", release=release(), check=refuse)
    finally:
        hosting.version_of = real_version
        web.shutdown()
        web.server_close()

    # an archive's member paths are never used: "../escape" goes nowhere
    work = os.path.join(TMP, "x", "y")
    os.makedirs(work)
    tgz = os.path.join(work, "evil.tgz")
    with tarfile.open(tgz, "w:gz") as t:
        for member, data in (("../escape", b"evil"), ("cloudflared", b"good")):
            info = tarfile.TarInfo(member)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    out = os.path.join(work, "out")
    hosting._extract(tgz, out)
    assert open(out, "rb").read() == b"good"
    assert not os.path.exists(os.path.join(TMP, "x", "escape"))
    with tarfile.open(tgz, "w:gz") as t:
        info = tarfile.TarInfo("README")
        info.size = 1
        t.addfile(info, io.BytesIO(b"x"))
    try:
        hosting._extract(tgz, out)
        raise AssertionError("extracted from an archive with no cloudflared")
    except hosting.DownloadError:
        pass
    print("download: checked against the release's SHA-256 and the signature; any failure "
          "leaves the copy that was there; archive paths go nowhere")


def signatures():
    """Something signed, but not by Cloudflare, is refused."""
    other = {"darwin": "/bin/ls",
             "win32": os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                   "System32", "notepad.exe")}.get(sys.platform)
    if other is None:
        hosting.verify_signature(FAKE_PATH)    # nothing to check on this platform
        print("signature: none to check on this platform")
        return
    try:
        hosting.verify_signature(other)
        raise AssertionError(f"{other} passed for Cloudflare's")
    except hosting.DownloadError as e:
        assert "not signed by Cloudflare" in str(e), e
    print(f"signature: {other}, signed by its own maker, is refused")


def signature_check():
    """Windows' check runs without PSModulePath, and a check that could not
    run says so instead of blaming the file. A StreamSync started from
    PowerShell 7 handed pwsh's module path on to Windows PowerShell, which
    then could not load Get-AuthenticodeSignature: the 2026-10-23 CI build
    threw away a cloudflared Cloudflare had signed."""
    env = hosting._signature_env(r"C:\x\cloudflared.exe",
                                 {"PSModulePath": r"C:\pwsh\Modules", "PSMODULEPATH": "a",
                                  "psmodulepath": "b", "Path": r"C:\Windows"})
    assert env == {"Path": r"C:\Windows", "SS_SIGNED": r"C:\x\cloudflared.exe"}, env
    said = hosting._first_error(
        '#< CLIXML\n<Objs Version="1.1.0.1"></Objs>\n'
        "Get-AuthenticodeSignature : The 'Get-AuthenticodeSignature' command was found in the "
        "module\n'Microsoft.PowerShell.Security', but the module could not be loaded.\n"
        "At line:1 char:6\n+ $s = Get-AuthenticodeSignature -LiteralPath $env:SS_SIGNED")
    assert said == ("Get-AuthenticodeSignature : The 'Get-AuthenticodeSignature' command was "
                    "found in the module 'Microsoft.PowerShell.Security', but the module could "
                    "not be loaded."), said
    real = hosting._authenticode
    cases = [(("", "", "the module could not be loaded"), "could not check"),
             (("Valid", 'CN="Cloudflare, Inc.", O="Cloudflare, Inc."', ""), None),
             (("Valid", "CN=Microsoft Windows, O=Microsoft Corporation", ""),
              "not signed by Cloudflare"),
             (("HashMismatch", 'CN="Cloudflare, Inc."', ""), "not signed by Cloudflare")]
    try:
        for answer, refusal in cases:
            hosting._authenticode = lambda path, a=answer: a
            try:
                hosting.verify_signature(r"C:\x\cloudflared.exe", plat="win32")
                assert refusal is None, f"{answer} passed"
            except hosting.DownloadError as e:
                assert refusal and refusal in str(e), (answer, e)
                assert e.detail == answer[2], (answer, e.detail)
    finally:
        hosting._authenticode = real
    if sys.platform != "win32":
        print("signature check: no PSModulePath, and 'could not check' is a reason of its own")
        return
    # the real thing: Windows PowerShell handed PowerShell 7's module path
    pwsh = shutil.which("pwsh")
    if not pwsh:
        print("signature check: no pwsh here to borrow a module path from")
        return
    p7 = subprocess.run([pwsh, "-NoProfile", "-Command", "$env:PSModulePath"],
                        capture_output=True, text=True, timeout=120).stdout.strip()
    notepad = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32",
                           "notepad.exe")
    old = os.environ.get("PSModulePath")
    os.environ["PSModulePath"] = p7
    try:
        status, signer, error = hosting._authenticode(notepad)
    finally:
        if old is None:
            os.environ.pop("PSModulePath", None)
        else:
            os.environ["PSModulePath"] = old
    assert status == "Valid" and signer.startswith("CN=Microsoft"), (status, signer, error, p7)
    print("signature check: Windows PowerShell reads a signature with pwsh's module path "
          "inherited")


def name_waits():
    answers = iter([{"Status": 3}, {"Status": 0, "Answer": [{"data": "104.16.0.1"}]}])

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    real = hosting.netcerts.urlopen
    hosting.netcerts.urlopen = lambda req, timeout=30: Reply(json.dumps(next(answers)).encode())
    try:
        t0 = time.monotonic()
        assert hosting.wait_for_name("x.trycloudflare.com")
        assert time.monotonic() - t0 < 3
        assert not hosting.wait_for_name("x", cancelled=lambda: True)
    finally:
        hosting.netcerts.urlopen = real
    print("wait_for_name: on until DNS has the name, and stops when cancelled")


def local_relay():
    r = hosting.LocalRelay()
    port = r.start()
    assert r.address == ("127.0.0.1", port), r.address
    assert room_works(f"ws://127.0.0.1:{port}")
    r.stop()
    assert refused(port), "the port stayed open"
    assert not relay_server.rooms, "rooms survived the relay"
    r2 = hosting.LocalRelay()
    assert room_works(f"ws://127.0.0.1:{r2.start()}")
    r2.stop()
    print("relay inside StreamSync: on 127.0.0.1 only; rooms work; stop closes it")


def tunnels():
    mode("ok")
    t = hosting.QuickTunnel(FAKE_EXE, 1)
    assert t.start(timeout=20) == "https://fake-able-mind-words.trycloudflare.com"
    assert t.alive()
    t.stop()
    assert not t.alive()

    mode("fail")
    try:
        hosting.QuickTunnel(FAKE_EXE, 1).start(timeout=20)
        raise AssertionError("a failed cloudflared opened")
    except RuntimeError as e:
        assert "failed to request quick Tunnel: 429 Too Many Requests" in str(e), e

    mode("silent")
    t = hosting.QuickTunnel(FAKE_EXE, 1)
    try:
        t.start(timeout=1.5)
        raise AssertionError("a silent cloudflared opened")
    except RuntimeError as e:
        assert "did not open within 2 s" in str(e), e
    assert not t.alive(), "a tunnel that timed out was left running"

    mode("die")
    down = threading.Event()
    t = hosting.QuickTunnel(FAKE_EXE, 1)
    t.on_exit = down.set
    t.start(timeout=20)
    assert down.wait(10), "on_exit never ran"
    print("tunnel: its address once registered; cloudflared's own error; a time limit; "
          "on_exit when it ends by itself")


def hostings():
    real_wait = hosting.wait_for_name
    hosting.wait_for_name = lambda *a, **k: True
    try:
        mode("ok")
        h = hosting.Hosting()
        said = []
        assert h.start(FAKE_EXE, said.append) == FAKE_WSS
        assert h.local_url.startswith("ws://127.0.0.1:") and len(said) == 3, said
        assert room_works(h.local_url)
        port = h.relay.port
        h.stop()
        assert not h.alive() and refused(port)

        # stopped while it opens: Cancelled, and nothing left behind
        mode("slow")
        h = hosting.Hosting()
        out = []

        def start():
            try:
                out.append(h.start(FAKE_EXE))
            except BaseException as e:
                out.append(e)
        th = threading.Thread(target=start)
        th.start()
        wait(lambda: h.tunnel is not None and h.tunnel.proc is not None, 10, "cloudflared")
        h.stop()
        th.join(10)
        assert out and isinstance(out[0], hosting.Cancelled), out
        assert not h.alive() and refused(h.relay.port)
    finally:
        hosting.wait_for_name = real_wait
    print("Hosting: relay and tunnel together; stop() cancels one still opening")


# ------------------------------------------------------------- controller

class Player:
    def stop(self):
        pass


class HostStub:
    def __init__(self, relay_url, *a, **kw):
        self.relay_url, self.code = relay_url, "ABCD-EFGH"
        self.stop_flag = threading.Event()

    def start(self):
        pass

    def stop(self):
        self.stop_flag.set()


class Log:
    """controller.diagnostics, kept out of the real streamsync.log."""
    def __init__(self):
        self.lines = []

    def log(self, message, echo=True):
        self.lines.append(message)

    def log_block(self, title, text):
        self.lines.append(title)


def controller_part():
    real = (hosting.wait_for_name, session.HostSession, controller.CONFIG_PATH,
            controller.diagnostics)
    hosting.wait_for_name = lambda *a, **k: True
    session.HostSession = HostStub
    controller.CONFIG_PATH = type(controller.CONFIG_PATH)(os.path.join(TMP, "config.json"))
    controller.diagnostics = log = Log()
    try:
        mode("ok")
        q = queue.Queue()
        ctl = controller.SyncController(q, Player())
        ctl.video_path = "film.mkv"
        ctl.relay_url = "wss://my-own-relay.example"

        local, public = ctl.open_tunnel(ctl.begin_hosting(), FAKE_EXE, False,
                                        lambda text: None)
        assert public == FAKE_WSS and ctl.hosting is not None
        ctl.host(local, None, False, None, 10.0, "film", remember_relay=False)
        assert ctl.relay_url == "wss://my-own-relay.example", "the party's address was saved"
        ctl.public_relay = public

        # cloudflared ending by itself mid-party is said
        ctl.hosting.tunnel.proc.kill()
        deadline = time.monotonic() + 10
        said = None
        while said is None and time.monotonic() < deadline:
            kind, *rest = q.get(timeout=10)
            if kind == "session":
                said = rest[0]
        assert said and "tunnel stopped" in said, said

        # Leave stops the session, then the relay and the tunnel
        h = ctl.hosting
        ctl.leave()
        assert ctl.hosting is None and ctl.public_relay is None
        wait(lambda: not h.alive() and refused(h.relay.port), 10, "the relay to close")

        # Leave while the tunnel opens stops it; so does quitting, at once
        for how in ("leave", "close"):
            local, public = ctl.open_tunnel(ctl.begin_hosting(), FAKE_EXE, False,
                                            lambda text: None)
            h = ctl.hosting
            getattr(ctl, how)()
            wait(lambda: not h.alive(), 5 if how == "leave" else 1, f"{how} to end cloudflared")

        # cancelled before the worker even starts: nothing opens at all
        h = ctl.begin_hosting()
        ctl.stop_hosting()
        try:
            ctl.open_tunnel(h, FAKE_EXE, False, lambda text: None)
            raise AssertionError("a cancelled hosting opened")
        except hosting.Cancelled:
            pass
        assert h.relay.port is None and h.tunnel is None, "it started something"

        # the choice is saved
        ctl.host_via = "relay"
        ctl.save_config({})
        ctl2 = controller.SyncController(queue.Queue(), Player())
        ctl2.load_config()
        assert ctl2.host_via == "relay"
        assert any("hosting: relay at ws://127.0.0.1:" in l for l in log.lines), log.lines
    finally:
        (hosting.wait_for_name, session.HostSession, controller.CONFIG_PATH,
         controller.diagnostics) = real
    print("controller: host_here's relay is not remembered; a dead tunnel is said; Leave "
          "and quit end cloudflared; the tunnel choice is saved")


def invites():
    text = hosting.invite(FAKE_WSS, "QSC9-X832")
    assert hosting.parse_invite(text) == (FAKE_WSS, "QSC9-X832"), "the tunnel's words are no code"
    assert hosting.parse_invite("  qsc9-x832 ") == (None, "QSC9-X832")
    assert hosting.parse_invite("https://a-b.trycloudflare.com.") == (
        "https://a-b.trycloudflare.com", None)
    assert hosting.parse_invite("") == (None, None)
    print("invites: one line out, address and code back")


def main():
    try:
        names_and_ages()
        finding()
        downloading()
        signatures()
        signature_check()
        name_waits()
        local_relay()
        tunnels()
        hostings()
        controller_part()
        invites()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("TUNNEL TEST PASSED")


if __name__ == "__main__":
    main()
