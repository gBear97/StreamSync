"""Checks the update logic that can be exercised without a Mac.

Covers version comparison, picking each platform's build, the checksum
lookup, the release signature (against RFC 8032's own test vectors, then
a whole download served from memory and refused every way it should be),
and where a Windows install may update itself from - the parts that
decide whether an update happens at all. Mounting and swapping a bundle
needs real macOS, and running the installer real Windows, so neither is
done here.

    python3 test_updater.py
"""

import updater

fails = []


def check(label, got, want):
    if got != want:
        fails.append(f"{label}: got {got!r}, wanted {want!r}")


# --- version parsing and ordering ---------------------------------------
check("plain", updater.parse_version("1.2.3"), (1, 2, 3))
check("v prefix", updater.parse_version("v1.2.3"), (1, 2, 3))
check("short", updater.parse_version("2.1"), (2, 1, 0))
check("junk", updater.parse_version("v1.0.0-beta2"), (1, 0, 2))
check("empty", updater.parse_version(""), (0, 0, 0))

check("newer patch", updater.is_newer("1.0.1", "1.0.0"), True)
check("newer minor", updater.is_newer("1.1.0", "1.0.9"), True)
check("newer major", updater.is_newer("2.0.0", "1.9.9"), True)
check("same", updater.is_newer("1.0.0", "1.0.0"), False)
check("older", updater.is_newer("0.9.9", "1.0.0"), False)
check("v-prefixed tag vs bare", updater.is_newer("v1.0.1", "1.0.0"), True)
# 10 must sort above 9, which a string compare would get wrong.
check("double digit", updater.is_newer("1.10.0", "1.9.0"), True)

# --- asset selection -----------------------------------------------------
ASSETS = [
    {"name": "StreamSync-1.1.0-arm64-abc1234.dmg"},
    {"name": "StreamSync-1.1.0-x86_64-abc1234.dmg"},
    {"name": "SHA256SUMS"},
]
check("arm64", updater.pick_asset(ASSETS, "arm64", "mac")["name"],
      "StreamSync-1.1.0-arm64-abc1234.dmg")
check("x86_64", updater.pick_asset(ASSETS, "x86_64", "mac")["name"],
      "StreamSync-1.1.0-x86_64-abc1234.dmg")

try:
    updater.pick_asset([{"name": "notes.txt"}], "arm64", "mac")
    fails.append("missing asset: should have raised UpdateError")
except updater.UpdateError:
    pass

# x86_64 must not be matched by an arm64 lookup, and vice versa.
try:
    updater.pick_asset([{"name": "StreamSync-1.1.0-x86_64-abc.dmg"}], "arm64",
                       "mac")
    fails.append("wrong arch: should have raised rather than cross-match")
except updater.UpdateError:
    pass

# Windows takes the installer, and each platform refuses the other's build
WIN = {"name": "StreamSync-2.0.0-windows-x64-abc1234-setup.exe"}
RELEASE = ASSETS + [WIN, {"name": "StreamSync-2.0.0-windows-x64-abc1234.zip"}]
check("windows installer", updater.pick_asset(RELEASE, plat="windows"), WIN)
for arch in ("arm64", "x86_64"):
    try:
        updater.pick_asset([WIN], arch, "mac")
        fails.append(f"a {arch} Mac picked the Windows installer")
    except updater.UpdateError:
        pass
try:
    updater.pick_asset(ASSETS, plat="windows")
    fails.append("Windows picked a disk image")
except updater.UpdateError:
    pass
check("platform here", updater.current_platform(),
      "windows" if updater.sys.platform == "win32" else "mac")

# --- checksum lookup -----------------------------------------------------
SUMS = (
    "aaaa1111  StreamSync-1.1.0-arm64-abc1234.dmg\n"
    "bbbb2222  StreamSync-1.1.0-x86_64-abc1234.dmg\n"
)
check("sha arm64", updater.expected_sha256(SUMS, "StreamSync-1.1.0-arm64-abc1234.dmg"),
      "aaaa1111")
check("sha x86_64", updater.expected_sha256(SUMS, "StreamSync-1.1.0-x86_64-abc1234.dmg"),
      "bbbb2222")
check("sha absent", updater.expected_sha256(SUMS, "StreamSync-9.9.9-arm64-zzz.dmg"), None)
# sha256sum writes "*name" for binary mode, and paths may be prefixed.
check("sha with dir", updater.expected_sha256(
    "cccc3333  dist/StreamSync-1.1.0-arm64-abc1234.dmg\n",
    "StreamSync-1.1.0-arm64-abc1234.dmg"), "cccc3333")

# --- running from source must never self-replace -------------------------
check("source checkout is not a bundle", updater.installed_app_path(), None)

# --- signature verification ---------------------------------------------
# codesign is macOS-only, so stub it out and check the decisions this
# makes from its output. The point is that anything short of an intact
# signature from our own team is refused.
import subprocess as _sp

_real_run = _sp.run


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _fake_codesign(verify_rc=0, team="52T7L6ZVY8", verify_err=""):
    def run(cmd, *a, **kw):
        if cmd[0] != "codesign":
            return _real_run(cmd, *a, **kw)
        if "--verify" in cmd:
            return _Result(verify_rc, "", verify_err)
        # the -dv identity probe; codesign reports on stderr
        body = "Identifier=StreamSync\n"
        if team is not None:
            body += f"TeamIdentifier={team}\n"
        else:
            body += "TeamIdentifier=not set\n"
        return _Result(0, "", body)
    return run


def _expect_refusal(label, **kw):
    _sp.run = _fake_codesign(**kw)
    try:
        updater.verify_signature("/tmp/whatever.app")
        fails.append(f"{label}: should have raised UpdateError")
    except updater.UpdateError:
        pass
    finally:
        _sp.run = _real_run


_sp.run = _fake_codesign()
try:
    check("intact signature from our team is accepted",
          updater.verify_signature("/tmp/whatever.app"), True)
finally:
    _sp.run = _real_run

# A broken or tampered signature.
_expect_refusal("broken signature", verify_rc=1,
                verify_err="a sealed resource is missing or invalid")
# Correctly signed, but by somebody else - the case a checksum cannot catch.
_expect_refusal("signed by another team", team="ABCDE12345")
# Ad-hoc signed, which is what an unsigned PyInstaller build looks like.
_expect_refusal("ad-hoc / unsigned", team=None)

_sp.run = _fake_codesign(team="ABCDE12345")
try:
    check("signing_team reads the team out", updater.signing_team("/tmp/x.app"),
          "ABCDE12345")
finally:
    _sp.run = _real_run

# --- the release signature (sigcheck) -----------------------------------
import hashlib
import os
import tempfile

import sigcheck

# RFC 8032 section 7.1, tests 1-3: public key, message, signature
RFC = [
    ("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
     "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
     "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
     "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
     "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]
for i, (pk, msg, sig) in enumerate(RFC, 1):
    pk, msg, sig = bytes.fromhex(pk), bytes.fromhex(msg), bytes.fromhex(sig)
    check(f"RFC 8032 test {i}", sigcheck.verify(pk, msg, sig), True)
    check(f"RFC test {i}, message changed",
          sigcheck.verify(pk, msg + b"x", sig), False)
    bad = bytearray(sig)
    bad[40] ^= 1
    check(f"RFC test {i}, signature changed",
          sigcheck.verify(pk, msg, bytes(bad)), False)
pk1, sig1 = bytes.fromhex(RFC[0][0]), bytes.fromhex(RFC[0][2])
check("another key's signature", sigcheck.verify(
    bytes.fromhex(RFC[1][0]), b"", sig1), False)
for label, args in (("short key", (pk1[:31], b"", sig1)),
                    ("short signature", (pk1, b"", sig1[:63])),
                    ("not bytes", ("key", b"", sig1)),
                    ("s out of range", (pk1, b"", sig1[:32] + b"\xff" * 32))):
    check(label, sigcheck.verify(*args), False)

# the app's own release key must at least be a point on the curve
check("release key length", len(updater.RELEASE_KEY), 32)
check("release key is a curve point",
      sigcheck._decompress(updater.RELEASE_KEY) is not None, True)


def _compress(point):
    zi = sigcheck._inv(point[2])
    x, y = point[0] * zi % sigcheck.P, point[1] * zi % sigcheck.P
    return (y | (x & 1) << 255).to_bytes(32, "little")


def _keypair(seed):
    """RFC 8032 signing, for these tests only - the app never signs."""
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little") & ((1 << 254) - 8) | (1 << 254)
    pub = _compress(sigcheck._mul(a, sigcheck.G))

    def sign(msg):
        r = int.from_bytes(hashlib.sha512(h[32:] + msg).digest(),
                           "little") % sigcheck.L
        big_r = _compress(sigcheck._mul(r, sigcheck.G))
        k = int.from_bytes(hashlib.sha512(big_r + pub + msg).digest(),
                           "little") % sigcheck.L
        return big_r + ((r + k * a) % sigcheck.L).to_bytes(32, "little")
    return pub, sign


# the signer reproduces RFC test 2 exactly, so its signatures are real ones
_pub2, _sign2 = _keypair(bytes.fromhex(
    "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"))
check("test signer matches RFC", (_pub2.hex(), _sign2(b"\x72").hex()),
      (RFC[1][0], RFC[1][2]))

# --- a whole verified download, served from memory ----------------------
PUB, SIGN = _keypair(b"\x01" * 32)
BUILD = b"pretend installer bytes " * 4096
NAME = WIN["name"]


def _release(sums, sig, build=BUILD):
    served = {"https://x/sums": sums, "https://x/build": build}
    if sig is not None:
        served["https://x/sig"] = sig
    assets = [{"name": NAME, "size": len(build),
               "browser_download_url": "https://x/build"},
              {"name": updater.SUMS_ASSET,
               "browser_download_url": "https://x/sums"}]
    if sig is not None:
        assets.append({"name": updater.SIG_ASSET,
                       "browser_download_url": "https://x/sig"})
    return {"assets": assets}, served


class _Response:
    def __init__(self, data):
        self.data, self.at = data, 0

    def read(self, n=-1):
        chunk = self.data[self.at:] if n < 0 else self.data[self.at:self.at + n]
        self.at += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _download(release, served):
    """download_verified against `served`, with PUB as the release key."""
    real = (updater._get, updater.netcerts.urlopen, updater.RELEASE_KEY)
    updater._get = lambda url, timeout=30: served[url]
    updater.netcerts.urlopen = lambda req, timeout=60: _Response(
        served[req.full_url])
    updater.RELEASE_KEY = PUB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = updater.download_verified(release["assets"][0], release,
                                             tmp)
            return open(path, "rb").read()
    finally:
        updater._get, updater.netcerts.urlopen, updater.RELEASE_KEY = real


def _expect_refused(label, release, served, says):
    try:
        _download(release, served)
        fails.append(f"{label}: should have been refused")
    except updater.UpdateError as e:
        if says not in str(e):
            fails.append(f"{label}: refused for the wrong reason: {e}")


SUMS_OK = (f"{hashlib.sha256(BUILD).hexdigest()}  {NAME}\n"
           "aaaa1111  StreamSync-2.0.0-arm64-abc1234.dmg\n").encode()
rel, served = _release(SUMS_OK, SIGN(SUMS_OK))
check("signed, matching download installs", _download(rel, served) == BUILD,
      True)

# the checksums edited after signing: nothing in them can be trusted
forged = SUMS_OK.replace(SUMS_OK[:4], b"0000")
_expect_refused("sums altered after signing", *_release(forged, SIGN(SUMS_OK)),
                "not signed by StreamSync's release key")
# signed by somebody else's key
_, other_sign = _keypair(b"\x02" * 32)
_expect_refused("signed with another key",
                *_release(SUMS_OK, other_sign(SUMS_OK)),
                "not signed by StreamSync's release key")
# a release with no signature at all, as every release before this had
_expect_refused("unsigned release", *_release(SUMS_OK, None),
                "no signed SHA256SUMS")
# the signature is fine but the build is not the one it vouches for
_expect_refused("swapped build", *_release(SUMS_OK, SIGN(SUMS_OK),
                                           build=BUILD + b"!"),
                "Checksum mismatch")
# signed sums that simply leave the build out
partial = b"aaaa1111  StreamSync-2.0.0-arm64-abc1234.dmg\n"
_expect_refused("build missing from sums", *_release(partial, SIGN(partial)),
                "no SHA256SUMS entry")

# --- where a Windows install updates itself from -------------------------
LOCAL = os.path.join("C:\\", "Users", "someone", "AppData", "Local")
HOME = os.path.join(LOCAL, "Programs", "StreamSync")
INSTALLED = os.path.join(HOME, "StreamSync.exe")
check("installed copy", updater.installed_exe_path(
    INSTALLED, frozen=True, localappdata=LOCAL), os.path.abspath(INSTALLED))
check("source run", updater.installed_exe_path(
    INSTALLED, frozen=False, localappdata=LOCAL), None)
check("portable copy elsewhere", updater.installed_exe_path(
    os.path.join("D:\\", "StreamSync", "StreamSync.exe"), frozen=True,
    localappdata=LOCAL), None)
check("this test process", updater.installed_exe_path(), None)

# the installer is started detached, silent, and told to relaunch
_launched = []
_real_popen = _sp.Popen
_sp.Popen = lambda args, **kw: _launched.append((args, kw))
try:
    updater.install_and_relaunch_windows(r"C:\Temp\setup.exe")
finally:
    _sp.Popen = _real_popen
args, kw = _launched[0]
check("installer command", args, [r"C:\Temp\setup.exe", "/VERYSILENT",
                                  "/SUPPRESSMSGBOXES", "/NORESTART",
                                  "/relaunch=1"])
check("installer detached", kw.get("creationflags", 0) & 0x8, 0x8)

if fails:
    print("UPDATER TEST FAILED")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("UPDATER TEST PASSED")
