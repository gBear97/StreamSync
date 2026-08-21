"""A working set of CA roots, in a frozen app as well as from source.

A PyInstaller build ships no certificate store. Python verifies HTTPS
through OpenSSL, which looks for roots at the paths compiled into the
OpenSSL the *build* machine had, and macOS keeps its roots in the system
keychain, where OpenSSL never looks. So a shipped StreamSync.app had no
trust anchors at all and every HTTPS call failed with

    [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate

which is why the updater could never reach GitHub, and why the one-click
BlackHole install would have failed the same way.

Roots are taken from the first of these that yields any:

- whatever OpenSSL finds by itself - correct when run from a checkout on
  a normally configured Python, and the only source that respects a
  machine's own CA configuration;
- certifi, which the macOS build collects into the bundle;
- the macOS system keychain, exported with `security`. This is a last
  resort rather than a preference: it reads the roots but not the user's
  trust settings, so a root they have explicitly distrusted would come
  back. It needs nothing bundled, which is the point - it is what keeps
  the app working if certifi ever fails to be collected again.

If a request still fails to verify, the remaining sources are added and
it is tried once more. That is not belt-and-braces: on a network that
inspects TLS, the certificate really is signed by a root that IT put in
the system keychain and that certifi has never heard of, so the bundled
roots are correctly rejecting it and only the keychain can succeed.

Stdlib only apart from the optional certifi import, so the dependency
gate can use it before any package is installed.
"""

import os
import ssl
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

# Apple's own root store, then whatever an administrator has added.
_KEYCHAINS = [
    "/System/Library/Keychains/SystemRootCertificates.keychain",
    "/Library/Keychains/System.keychain",
]

_lock = threading.Lock()
_ctx = None
_widened = False


def _ca_count(ctx):
    """How many CA certificates this context has actually loaded.

    Zero can be a false alarm - a capath directory loads lazily and is
    not counted - but the only cost of a false alarm is loading roots we
    did not need, so it is the safe way to be wrong.
    """
    try:
        return ctx.cert_store_stats().get("x509_ca", 0)
    except Exception:
        return 0


def _load_certifi(ctx):
    try:
        import certifi
    except ImportError:
        return False
    try:
        ctx.load_verify_locations(cafile=certifi.where())
        return True
    except Exception:
        return False


def _load_keychain(ctx):
    """Load the macOS trust roots by exporting them to a temporary PEM.

    A no-op anywhere the keychains do not exist, which is what makes the
    callers below free of platform tests.
    """
    chunks = []
    for kc in _KEYCHAINS:
        if not os.path.exists(kc):
            continue
        try:
            r = subprocess.run(
                ["/usr/bin/security", "find-certificate", "-a", "-p", kc],
                capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and "BEGIN CERTIFICATE" in (r.stdout or ""):
            chunks.append(r.stdout)
    if not chunks:
        return False

    fd, path = tempfile.mkstemp(prefix="streamsync-ca-", suffix=".pem")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(chunks))
        # OpenSSL reads a cafile eagerly, so the file is not needed after
        # this returns and is removed rather than left in /tmp.
        ctx.load_verify_locations(cafile=path)
        return True
    except Exception:
        return False
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _build():
    ctx = ssl.create_default_context()
    if _ca_count(ctx) == 0:
        _load_certifi(ctx)
    if _ca_count(ctx) == 0:
        _load_keychain(ctx)
    return ctx


def context():
    """The shared verifying SSL context. Built once, safe from any thread."""
    global _ctx
    with _lock:
        if _ctx is None:
            _ctx = _build()
        return _ctx


def _widen():
    """Add the remaining root sources to the shared context, once.

    Only reached after a verification failure, because exporting the
    keychain costs a subprocess and is pointless when the roots we have
    already work. Returns whether it actually found anything new.
    """
    global _widened
    ctx = context()  # outside the lock: context() takes it too
    with _lock:
        if _widened:
            return False
        _widened = True
        before = _ca_count(ctx)
        _load_certifi(ctx)
        _load_keychain(ctx)
        return _ca_count(ctx) > before


def urlopen(req, timeout=30):
    """urlopen with roots that exist inside a frozen app.

    Every request here is a GET, so the retry is safe to repeat.
    """
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=context())
    except (urllib.error.URLError, ssl.SSLError) as e:
        if not isinstance(getattr(e, "reason", e), ssl.SSLError):
            raise
        if not _widen():
            raise
        return urllib.request.urlopen(req, timeout=timeout, context=context())


def describe():
    """Where the roots came from and how many - for --netcheck and bugs."""
    ctx = context()
    n = _ca_count(ctx)
    paths = ssl.get_default_verify_paths()
    try:
        import certifi
        where = certifi.where()
    except ImportError:
        where = None
    return {
        "ca_certs": n,
        "widened": _widened,
        "openssl_cafile": paths.openssl_cafile,
        "openssl_cafile_exists": bool(paths.openssl_cafile
                                      and os.path.exists(paths.openssl_cafile)),
        "certifi": where,
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def selfcheck(url="https://api.github.com/", out=print):
    """Prove this build can actually complete a verified HTTPS request.

    Run against the built .app in CI: nothing else catches a bundle that
    was packaged without usable roots, because from a source checkout the
    system's own store hides the problem completely.
    """
    info = describe()
    for k in sorted(info):
        out(f"  {k}: {info[k]}")
    if not info["ca_certs"]:
        out(f"FAIL: no CA roots available for {url}")
        return 1
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "StreamSync-netcheck"})
        with urlopen(req, timeout=30) as r:
            out(f"  HTTP {r.status} from {url}")
    except Exception as e:
        out(f"FAIL: {type(e).__name__}: {e}")
        return 1
    # Worth saying out loud: succeeding only after widening means the
    # bundled roots were not enough by themselves on this machine.
    if _widened:
        out(f"  (needed the fallback roots; now {_ca_count(context())})")
    out("NETCHECK OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(selfcheck())
