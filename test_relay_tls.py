"""A watch party relay at a wss:// address, verified the way the shipped
app verifies one.

The bug this guards: session.Link opened every relay with websockets'
defaults, so a wss:// relay - a Cloudflare tunnel's, say - was verified
with Python's default SSL context. In the shipped Mac app that context
has no CA roots at all (see netcerts.py), so the connection failed with
CERTIFICATE_VERIFY_FAILED and the watch party said only "could not reach
the relay". Run from a checkout whose Python has its own roots, nothing
showed.

  - which context each scheme gets, with websockets' connect stubbed:
    wss:// netcerts' shared one, ws:// none at all, as before;
  - the widen-and-retry netcerts.urlopen does, for a websocket;
  - a real round trip through relay_server.py behind a TLS front, as a
    tunnel puts one in front of it, with a throwaway CA standing in for
    the bundled roots and OpenSSL's own CA paths taken away - the shipped
    app's situation. Also the TLS-inspecting network, where only the
    keychain's roots can verify, and a certificate no root vouches for,
    which must still be refused.

The round trip needs the openssl command to make certificates; where
there is none (some Windows setups) it is skipped and says so.

    python3 test_relay_tls.py
"""

import asyncio
import os
import queue
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time

import websockets.sync.client as wsclient

import netcerts
import session

fails = []


def check(label, got, want):
    if got != want:
        fails.append(f"{label}: got {got!r}, wanted {want!r}")


def ok(label, cond):
    if not cond:
        fails.append(label)


# --- which context each scheme gets -------------------------------------

class FakeWs:
    def __iter__(self):
        return iter(())        # the pump thread ends at once

    def send(self, data):
        pass

    def close(self):
        pass


class FakeConnect:
    """Stands in for websockets' connect; records what it was given."""

    def __init__(self, fail_first=None):
        self.calls = []
        self.fail_first = fail_first

    def __call__(self, url, **kw):
        self.calls.append((url, kw))
        if self.fail_first is not None and len(self.calls) == 1:
            raise self.fail_first
        return self

    def __enter__(self):
        return FakeWs()


real_connect = wsclient.connect
real_widen = netcerts._widen
try:
    for url in ("wss://relay.example", "WSS://relay.example:443/room",
                "wss://birmingham-imagine.trycloudflare.com"):
        fake = wsclient.connect = FakeConnect()
        link = session.Link(url, queue.Queue())
        url_, kw = fake.calls[0]
        check(f"{url}: opened once", len(fake.calls), 1)
        ok(f"{url}: given netcerts' shared context",
           kw.get("ssl") is netcerts.context())
        check(f"{url}: max_size", kw.get("max_size", "absent"), None)
        check(f"{url}: open_timeout", kw.get("open_timeout"), 10)
        link.close()

    # ws:// exactly as before: websockets refuses an ssl argument there.
    for url in ("ws://localhost:8765", "ws://203.0.113.7:8765",
                "ws://birmingham-imagine.trycloudflare.com"):
        fake = wsclient.connect = FakeConnect()
        session.Link(url, queue.Queue()).close()
        check(f"{url}: arguments", fake.calls,
              [(url, {"max_size": None, "open_timeout": 10})])

    # A failure to verify widens the roots and tries once more ...
    widened = []
    netcerts._widen = lambda: widened.append(1) or True
    fake = wsclient.connect = FakeConnect(
        ssl.SSLCertVerificationError("certificate verify failed"))
    try:
        session._open("wss://relay.example")
    except Exception as e:
        fails.append(f"a verification failure was not retried: {e!r}")
    check("retried once after widening", (len(fake.calls), len(widened)),
          (2, 1))

    # ... but not when widening found nothing new ...
    netcerts._widen = lambda: False
    fake = wsclient.connect = FakeConnect(
        ssl.SSLCertVerificationError("certificate verify failed"))
    try:
        session._open("wss://relay.example")
        fails.append("a verification failure with nothing to widen passed")
    except ssl.SSLCertVerificationError:
        pass
    check("no retry with nothing new to trust", len(fake.calls), 1)

    # ... and a relay that is simply not there is not a TLS problem.
    netcerts._widen = lambda: widened.append(1) or True
    widened.clear()
    fake = wsclient.connect = FakeConnect(ConnectionRefusedError("nope"))
    try:
        session._open("wss://relay.example")
        fails.append("a refused connection was swallowed")
    except ConnectionRefusedError:
        pass
    check("no retry or widening on a refused connection",
          (len(fake.calls), len(widened)), (1, 0))
finally:
    wsclient.connect = real_connect
    netcerts._widen = real_widen

# --- nothing else opens a websocket --------------------------------------
# session._open is the one place a relay connection gets its roots, so a
# second client elsewhere would bring the bug back on a Mac only.
here = os.path.dirname(os.path.abspath(__file__))
offenders = []
for name in sorted(os.listdir(here)):
    if (not name.endswith(".py") or name.startswith("test_")
            or name == "session.py"):
        continue
    with open(os.path.join(here, name), encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if ("client import connect" in line
                    or "websockets.connect(" in line
                    or "client.connect(" in line):
                offenders.append(f"{name}:{n}")
check("no websocket client outside session._open", offenders, [])


# --- a real round trip over TLS ------------------------------------------

OPENSSL_CNF = """\
[req]
distinguished_name = dn
prompt = no
[dn]
CN = StreamSync test
[ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
[leaf]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:localhost, IP:127.0.0.1
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
"""


def make_certs(d):
    """A throwaway CA, a relay certificate it signed, and a self-signed
    one nobody vouches for. None of it is in any real trust store."""
    cnf = os.path.join(d, "openssl.cnf")
    with open(cnf, "w") as f:
        f.write(OPENSSL_CNF)
    p = {k: os.path.join(d, k) for k in
         ("ca.pem", "ca.key", "relay.csr", "relay.pem", "relay.key",
          "stranger.pem", "stranger.key")}
    steps = [
        ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-subj", "/CN=StreamSync test CA", "-config", cnf,
         "-extensions", "ca", "-keyout", p["ca.key"], "-out", p["ca.pem"]],
        ["req", "-new", "-newkey", "rsa:2048", "-nodes",
         "-subj", "/CN=localhost", "-config", cnf,
         "-keyout", p["relay.key"], "-out", p["relay.csr"]],
        ["x509", "-req", "-in", p["relay.csr"], "-CA", p["ca.pem"],
         "-CAkey", p["ca.key"], "-set_serial", "2", "-days", "2",
         "-extfile", cnf, "-extensions", "leaf", "-out", p["relay.pem"]],
        ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-subj", "/CN=localhost", "-config", cnf, "-extensions", "leaf",
         "-keyout", p["stranger.key"], "-out", p["stranger.pem"]],
    ]
    for args in steps:
        try:
            r = subprocess.run(["openssl"] + args, capture_output=True,
                               text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            return None, f"openssl could not run: {e}"
        if r.returncode != 0:
            return None, f"openssl {args[0]} failed: {r.stderr.strip()}"
    return p, None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tls_front(certfile, keyfile, backend_port):
    """Terminate TLS and pass the bytes through to the plain relay - what
    a tunnel does in front of one. Returns the port it listens on."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)
    ready, port = threading.Event(), []

    async def pipe(r, w):
        try:
            while data := await r.read(65536):
                w.write(data)
                await w.drain()
        except Exception:
            pass
        finally:
            w.close()

    async def handle(cr, cw):
        try:
            br, bw = await asyncio.open_connection("127.0.0.1", backend_port)
        except OSError:
            cw.close()
            return
        await asyncio.gather(pipe(cr, bw), pipe(br, cw))

    async def serve():
        srv = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
        port.append(srv.sockets[0].getsockname()[1])
        ready.set()
        await srv.serve_forever()

    threading.Thread(target=asyncio.run, args=(serve(),),
                     daemon=True).start()
    if not ready.wait(10):
        raise RuntimeError("the TLS front did not start")
    return port[0]


def wait_for_port(port, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"nothing listening on {port}")


def rebuild(certifi=None, keychain=None, widened=False):
    """A fresh shared context from the given root sources (None: the real
    one), as the app builds it on first use."""
    netcerts._load_certifi = certifi or real_certifi
    netcerts._load_keychain = keychain or real_keychain
    netcerts._ctx = None
    netcerts._widened = widened
    netcerts.context()


def first(inbox, want, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            msg = inbox.get(timeout=0.5)
        except queue.Empty:
            continue
        if msg.get("type") == want:
            return msg
    raise AssertionError(f"no {want!r} from the relay")


def party(url):
    """Host and viewer through the relay, the way the sessions do it."""
    stop = threading.Event()
    deadline = time.monotonic() + 20
    hq, vq = queue.Queue(), queue.Queue()
    host, made = session._connect(
        url, hq, {"type": "create", "password": "swordfish",
                  "meta": {"title": "tls film"}},
        ("created",), stop, deadline)
    viewer, joined = session._connect(
        url, vq, {"type": "join", "code": made["code"],
                  "password": "swordfish"},
        ("joined",), stop, deadline)
    try:
        check("the viewer sees the host's film", joined["meta"],
              {"title": "tls film"})
        host.send({"type": "state", "ck": "state", "seq": 1, "pos": 42.5,
                   "utc": 1.0, "playing": True})
        check("host -> viewer", first(vq, "state")["pos"], 42.5)
        viewer.send({"type": "verified", "ok": True})
        ok("viewer -> host", first(hq, "verified")["ok"])
        host.send({"type": "end"})
        first(vq, "ended")
    finally:
        host.close()
        viewer.close()


real_certifi = netcerts._load_certifi
real_keychain = netcerts._load_keychain
saved_env = {k: os.environ.get(k) for k in ("SSL_CERT_FILE", "SSL_CERT_DIR")}
tmp = tempfile.mkdtemp()
certs, why = make_certs(tmp)
if certs is None:
    print(f"  TLS round trip SKIPPED: {why}")
    if sys.platform != "win32":
        fails.append(f"the TLS round trip could not run: {why}")
else:
    relay_port = free_port()
    relay = subprocess.Popen([sys.executable, "relay_server.py",
                              "--port", str(relay_port)], cwd=here)
    try:
        wait_for_port(relay_port)
        url = f"wss://127.0.0.1:{tls_front(certs['relay.pem'], certs['relay.key'], relay_port)}"
        stranger = (f"wss://127.0.0.1:"
                    f"{tls_front(certs['stranger.pem'], certs['stranger.key'], relay_port)}")
        trust_ca = (lambda ctx: ctx.load_verify_locations(
            cafile=certs["ca.pem"]) or True)

        # The shipped Mac app: OpenSSL's own CA paths lead nowhere, and
        # the roots it does have are the ones bundled with it - here the
        # throwaway CA, standing in for certifi.
        os.environ["SSL_CERT_FILE"] = os.path.join(tmp, "no-such-cert.pem")
        os.environ["SSL_CERT_DIR"] = os.path.join(tmp, "no-such-certs")

        # What session.Link did before: websockets' own default context,
        # which cannot verify the relay. If it could, the rest of this
        # would pass with or without the fix and prove nothing.
        try:
            real_connect(url, open_timeout=5).__enter__().close()
            fails.append("Python's default context verified the test relay "
                         "by itself, so this test cannot see the bug")
        except ssl.SSLCertVerificationError:
            print("  default context: CERTIFICATE_VERIFY_FAILED, as in the "
                  "shipped Mac app")

        rebuild(certifi=trust_ca, keychain=lambda ctx: False)
        try:
            party(url)
            print(f"  wss:// round trip through relay_server.py "
                  f"(roots from {netcerts._source}, "
                  f"widened={netcerts._widened})")
        except Exception as e:
            fails.append(f"no wss:// watch party with the bundled roots: "
                         f"{e!r}")

        # A network that inspects TLS: the bundled roots correctly reject
        # its certificate and only a root IT put in the keychain accepts
        # it. Real roots to start with, the CA reachable only by widening.
        rebuild()
        netcerts._load_certifi = lambda ctx: False
        netcerts._load_keychain = trust_ca
        try:
            link = session.Link(url, queue.Queue())
            link.close()
            ok("got there by widening the roots", netcerts._widened)
            print("  wss:// after widening to the keychain's roots")
        except Exception as e:
            fails.append(f"widening did not rescue a wss:// relay: {e!r}")

        # Verification is still verification: a certificate no root
        # vouches for is refused, widened or not.
        try:
            session.Link(stranger, queue.Queue()).close()
            fails.append("a relay with an untrusted certificate was accepted")
        except ssl.SSLCertVerificationError:
            print("  untrusted certificate refused")
    finally:
        relay.terminate()
        relay.wait(10)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        rebuild()

if fails:
    print("RELAY TLS TEST FAILED")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("RELAY TLS TEST PASSED")
