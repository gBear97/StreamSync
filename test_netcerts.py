"""Checks that HTTPS out of StreamSync has CA roots to verify against.

The bug this guards: a PyInstaller build shipped no certificate store, so
every HTTPS call from the installed .app died with CERTIFICATE_VERIFY_
FAILED and the updater never once reached GitHub. Running from a source
checkout hid it completely, because the system's own store was there.

So these tests do two things a source-checkout test can do:

  - exercise each root source on a context that starts empty, including
    with certifi made unavailable, which is the frozen app's situation;
  - refuse any new bare urlopen call, since bypassing netcerts is exactly
    how the bug comes back.

What they cannot do is prove the *built bundle* carries roots. That is
`StreamSync.app/Contents/MacOS/StreamSync --netcheck`, which CI runs
against the real app after building it.

    python3 test_netcerts.py
"""

import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

import netcerts

fails = []


def check(label, got, want):
    if got != want:
        fails.append(f"{label}: got {got!r}, wanted {want!r}")


def ok(label, cond):
    if not cond:
        fails.append(label)


def empty_context():
    """A context with no trust anchors, the way a frozen app starts out."""
    return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class no_certifi:
    """Make `import certifi` fail, to stand in for a bundle without it."""

    def __enter__(self):
        self.saved = sys.modules.get("certifi", "absent")
        sys.modules["certifi"] = None  # import raises ImportError
        return self

    def __exit__(self, *exc):
        if self.saved == "absent":
            sys.modules.pop("certifi", None)
        else:
            sys.modules["certifi"] = self.saved


# --- the context is actually verifying ----------------------------------
# A context that trusts everything would make the whole updater pointless,
# so assert the security properties, not just that a request succeeds.
ctx = netcerts.context()
check("verify mode", ctx.verify_mode, ssl.CERT_REQUIRED)
check("hostname checking", ctx.check_hostname, True)
ok("context is reused, not rebuilt per call", netcerts.context() is ctx)
ok("the shared context has CA roots", netcerts._ca_count(ctx) > 0)

# --- an empty context really is empty -----------------------------------
ok("empty context has no roots", netcerts._ca_count(empty_context()) == 0)

# --- certifi fills an empty context -------------------------------------
c = empty_context()
if netcerts._load_certifi(c):
    ok("certifi loaded roots", netcerts._ca_count(c) > 0)
else:
    fails.append("certifi is not importable - it is in requirements.txt and "
                 "the build collects it, so this environment is missing it")

with no_certifi():
    ok("certifi treated as absent when it cannot be imported",
       netcerts._load_certifi(empty_context()) is False)

# --- the reported source is the one that actually supplied the roots ----
# Two sources can yield the same certificate count, so the count alone
# cannot tell CI which one carried the request.
_saved_ctx, _saved_src = netcerts._ctx, netcerts._source
_orig_default = ssl.create_default_context
try:
    ssl.create_default_context = empty_context  # no roots from OpenSSL
    netcerts._ctx = None
    netcerts._build()
    check("source names certifi when OpenSSL has nothing",
          netcerts._source, "certifi")
finally:
    ssl.create_default_context = _orig_default
    netcerts._ctx, netcerts._source = _saved_ctx, _saved_src

# --- the keychain fallback, which needs nothing bundled -----------------
if sys.platform == "darwin":
    c = empty_context()
    ok("keychain export loaded roots", netcerts._load_keychain(c))
    ok("keychain roots landed in the store", netcerts._ca_count(c) > 0)

    # The real point of the fallback: roots even with certifi gone.
    with no_certifi():
        netcerts._ctx = None
        try:
            ok("roots available with no certifi at all",
               netcerts._ca_count(netcerts._build()) > 0)
        finally:
            netcerts._ctx = None
else:
    ok("keychain paths are macOS-only",
       all(p.startswith("/System/") or p.startswith("/Library/")
           for p in netcerts._KEYCHAINS))

# --- a verification failure widens the roots and retries once -----------
# The case this covers is a Mac behind a TLS-inspecting proxy: the bundled
# certifi roots reject that certificate correctly, and only the root IT put
# in the system keychain can accept it. Stubbed so it runs anywhere.
_real_keychain = netcerts._load_keychain
_real_certifi = netcerts._load_certifi
_real_urlopen = urllib.request.urlopen


def _reset(widened=False):
    netcerts._ctx = None
    netcerts._widened = widened


def throwaway_root():
    """A CA certificate that is certainly not in any real trust store."""
    try:
        r = subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", "/dev/null", "-days", "1", "-subj",
             "/CN=StreamSync test root"],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if "BEGIN CERTIFICATE" in (r.stdout or "") else None


try:
    extra = throwaway_root()
    if extra is None:
        fails.append("openssl is unavailable, so the widening path was "
                     "not exercised")
    else:
        netcerts._load_keychain = lambda ctx: (
            ctx.load_verify_locations(cadata=extra) or True)
        # Certifi is already in the shared context, so silence it here:
        # then the only thing that can add a root is the keychain, and the
        # assertions below are about that source specifically.
        netcerts._load_certifi = lambda ctx: False
        _reset()
        base = netcerts._ca_count(netcerts.context())
        ok("widening finds roots the bundle did not have",
           netcerts._widen() is True)
        ok("the widened roots are in the shared context",
           netcerts._ca_count(netcerts.context()) > base)
        ok("widening happens at most once", netcerts._widen() is False)

    # A verification failure is retried; anything else is not.
    calls = []

    def flaky(req, timeout=None, context=None):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.URLError(
                ssl.SSLCertVerificationError("certificate verify failed"))
        return "response"

    _reset()
    urllib.request.urlopen = flaky
    try:
        check("retried after widening", netcerts.urlopen("req"), "response")
    except Exception as e:
        fails.append(f"a verification failure was not retried: {e!r}")
    check("exactly one retry", len(calls), 2)

    calls.clear()

    def refused(req, timeout=None, context=None):
        calls.append(1)
        raise urllib.error.URLError(ConnectionRefusedError("nope"))

    _reset()
    urllib.request.urlopen = refused
    try:
        netcerts.urlopen("req")
        fails.append("a non-TLS failure should not be retried or swallowed")
    except urllib.error.URLError:
        pass
    check("no retry on a non-TLS failure", len(calls), 1)
finally:
    netcerts._load_keychain = _real_keychain
    netcerts._load_certifi = _real_certifi
    urllib.request.urlopen = _real_urlopen
    _reset()

# --- an HTTP error is a verified connection, not a CA failure -----------
# selfcheck used to treat any exception as failure, so GitHub answering
# "403 rate limit exceeded" from a shared CI address failed the release -
# even though receiving an HTTP status at all proves the certificate
# verified.
_real_urlopen2 = urllib.request.urlopen


def _refusing(status):
    def f(req, timeout=None, context=None):
        raise urllib.error.HTTPError("https://x/", status, "nope", {}, None)
    return f


try:
    urllib.request.urlopen = _refusing(403)
    lines = []
    try:
        check("an HTTP refusal still passes the check",
              netcerts.selfcheck(out=lines.append), 0)
        ok("and it says the connection verified",
           any("verified" in ln for ln in lines))
    except Exception as e:
        fails.append(f"an HTTP refusal was not tolerated: {e!r}")

    def _tls_broken(req, timeout=None, context=None):
        raise urllib.error.URLError(
            ssl.SSLCertVerificationError("certificate verify failed"))

    _reset(widened=True)  # widened, so the retry cannot rescue it
    urllib.request.urlopen = _tls_broken
    check("a verification failure still fails the check",
          netcerts.selfcheck(out=lambda _: None), 1)
finally:
    urllib.request.urlopen = _real_urlopen2
    _reset()

# --- describe() reports enough to diagnose a repeat ---------------------
info = netcerts.describe()
for key in ("ca_certs", "source", "openssl_cafile", "certifi", "frozen"):
    ok(f"describe() reports {key}", key in info)

# --- nothing bypasses netcerts ------------------------------------------
# The original bug was one bare urlopen in updater.py and another in
# depcheck.py. Both are fine from source and both fail in the shipped app,
# so review will not catch a third - this does.
here = os.path.dirname(os.path.abspath(__file__))

# Deliberate exemptions, each of which must stay plain HTTP to be one.
ALLOWED = {
    "players.py": "plain HTTP to VLC's own interface on 127.0.0.1; there is "
                  "no TLS on that connection, so there is nothing to verify",
}

offenders = []
for name in sorted(os.listdir(here)):
    if not name.endswith(".py") or name in ("netcerts.py", "test_netcerts.py"):
        continue
    with open(os.path.join(here, name), encoding="utf-8") as f:
        text = f.read()
    hits = [n for n, line in enumerate(text.splitlines(), 1)
            if "urllib.request.urlopen(" in line and "netcerts" not in line]
    if not hits:
        continue
    if name in ALLOWED:
        # The exemption holds only while the file really has no HTTPS in it.
        ok(f"{name} is exempt but now contains an https:// URL - route it "
           f"through netcerts or drop the exemption", "https://" not in text)
        continue
    offenders += [f"{name}:{n}" for n in hits]
check("no bare urlopen outside netcerts", offenders, [])

if fails:
    print("NETCERTS TEST FAILED")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("NETCERTS TEST PASSED")
