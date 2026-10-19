"""Checks the update logic that can be exercised without a Mac.

Covers version comparison, picking each platform's build, the checksum
lookup, the release signature (against RFC 8032's own test vectors and
every malformed key, R and length it must refuse, then a whole download
served from memory and refused every way it should be), and where a
Windows install may update itself from - the parts that decide whether
an update happens at all. Mounting and swapping a bundle needs real
macOS, and running the installer real Windows, so neither is done here.

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
                    ("s = L", (pk1, b"", sig1[:32]
                               + sigcheck.L.to_bytes(32, "little"))),
                    ("s all ones", (pk1, b"", sig1[:32] + b"\xff" * 32))):
    check(label, sigcheck.verify(*args), False)

# Malleability (RFC 8032 5.1.7): (R, s + L) satisfies the group equation
# exactly as (R, s) does, since [L]B is the identity - only the s < L
# check turns it away. Each case first proves the equation still holds,
# so this fails if that check is dropped or loosened (say to s >= 2L).
for i, (pk, msg, sig) in enumerate(RFC, 1):
    pk, msg, sig = bytes.fromhex(pk), bytes.fromhex(msg), bytes.fromhex(sig)
    s_plus_l = int.from_bytes(sig[32:], "little") + sigcheck.L
    check(f"RFC test {i}, s + L still fits in 32 bytes",
          s_plus_l < 2 ** 256, True)
    malleated = sig[:32] + s_plus_l.to_bytes(32, "little")
    h = int.from_bytes(hashlib.sha512(sig[:32] + pk + msg).digest(),
                       "little") % sigcheck.L
    check(f"RFC test {i}, s + L passes the group equation",
          sigcheck._equal(sigcheck._mul(s_plus_l, sigcheck.G),
                          sigcheck._add(sigcheck._decompress(sig[:32]),
                                        sigcheck._mul(h, sigcheck._decompress(pk)))),
          True)
    check(f"RFC test {i}, s + L rejected", sigcheck.verify(pk, msg, malleated),
          False)

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

    def sign(msg, as_key=None, big_r=None):
        """`as_key` hashes other bytes in the public key's place, and
        `big_r` puts in a given R with no nonce behind it (r = 0): both
        only to make signatures the verifier has to turn away."""
        key = pub if as_key is None else as_key
        if big_r is None:
            r = int.from_bytes(hashlib.sha512(h[32:] + msg).digest(),
                               "little") % sigcheck.L
            big_r = _compress(sigcheck._mul(r, sigcheck.G))
        else:
            r = 0
        k = int.from_bytes(hashlib.sha512(big_r + key + msg).digest(),
                           "little") % sigcheck.L
        return big_r + ((r + k * a) % sigcheck.L).to_bytes(32, "little")
    return pub, sign


# the signer reproduces RFC test 2 exactly, so its signatures are real ones
_pub2, _sign2 = _keypair(bytes.fromhex(
    "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"))
check("test signer matches RFC", (_pub2.hex(), _sign2(b"\x72").hex()),
      (RFC[1][0], RFC[1][2]))

# --- how a key or an R is decoded (RFC 8032 5.1.3) ----------------------
# 32 bytes: y, with the low bit of x in the top bit. Each refusal below is
# shown first to matter - the bytes would name a point, or let a forgery
# through, if that one rule were gone.
IDENTITY = (0, 1, 1, 0)


def _encode(y, sign=0):
    return (y | sign << 255).to_bytes(32, "little")


def _on_curve(point):
    zi = sigcheck._inv(point[2])
    x, y = point[0] * zi % sigcheck.P, point[1] * zi % sigcheck.P
    return (y * y - x * x - 1 - sigcheck.D * x * x * y * y) % sigcheck.P == 0


def _verify(*args):
    """sigcheck.verify, with anything it raises reported, not raised: it
    promises never to raise on bad input."""
    try:
        return sigcheck.verify(*args)
    except Exception as e:
        return f"raised {type(e).__name__}"


def _needs_sqrt_m1(enc):
    """Whether x^2's (P+3)/8-th power, the first root tried, is not one
    and the decoder has to multiply it by sqrt(-1)."""
    y = int.from_bytes(enc, "little") & ((1 << 255) - 1)
    x2 = (y * y - 1) * sigcheck._inv(sigcheck.D * y * y + 1) % sigcheck.P
    x = pow(x2, (sigcheck.P + 3) // 8, sigcheck.P)
    return (x * x - x2) % sigcheck.P != 0


# Every RFC key and R decodes to a point on the curve that encodes back to
# the same bytes: the right root, with the right sign. Keys 1 and 3 take
# the sqrt(-1) step, key 2 does not.
for i, (pk, _, sig) in enumerate(RFC, 1):
    for what, enc in (("key", pk), ("R", sig[:64])):
        point = sigcheck._decompress(bytes.fromhex(enc))
        check(f"RFC test {i} {what} decodes onto the curve",
              point is not None and _on_curve(point)
              and _compress(point).hex(), enc)
check("RFC keys that need sqrt(-1)",
      [i for i, (pk, _, _) in enumerate(RFC, 1)
       if _needs_sqrt_m1(bytes.fromhex(pk))], [1, 3])

# The eight points of small order, the curve's cofactor: the identity,
# (0, -1), (+-sqrt(-1), 0) - which take the sqrt(-1) step - and four of
# order 8. Each is a proper encoding and decodes to just that point.
SMALL_ORDER = {
    "0100000000000000000000000000000000000000000000000000000000000000": 1,
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f": 2,
    "0000000000000000000000000000000000000000000000000000000000000000": 4,
    "0000000000000000000000000000000000000000000000000000000000000080": 4,
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a": 8,
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac03fa": 8,
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05": 8,
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc85": 8,
}
orders = []
for enc, order in SMALL_ORDER.items():
    t = sigcheck._decompress(bytes.fromhex(enc))
    ok = t is not None and _on_curve(t) and _compress(t).hex() == enc
    check(f"order-{order} point {enc[:4]}..{enc[-2:]} decodes", ok, True)
    orders.append(ok and next((k for k in (1, 2, 4, 8) if sigcheck._equal(
        sigcheck._mul(k, t), IDENTITY)), None))
check("small-order points' orders", orders, list(SMALL_ORDER.values()))
check("(+-sqrt(-1), 0) needs sqrt(-1)", _needs_sqrt_m1(bytes(32)), True)

# The release key is of order L - in the prime-order group, and not the
# identity - so it is no small-order key. RFC 8032 does not refuse those
# (with the identity as A, [k]A vanishes and (B, 1) verifies for any
# message), and neither does this verifier: the one key it checks
# against is fixed in the app, so no weak key can be slipped in.
release_a = sigcheck._decompress(updater.RELEASE_KEY)
check("release key has order L", release_a is not None
      and (sigcheck._equal(sigcheck._mul(sigcheck.L, release_a), IDENTITY),
           sigcheck._equal(release_a, IDENTITY)), (True, False))

# x = 0 has no sign to give: the identity or (0, -1) with the top bit set
# names nothing. A decoder that let "-0" by would give the identity two
# spellings, and the second lets that (B, 1) forgery through.
b_one = _compress(sigcheck.G) + (1).to_bytes(32, "little")
for enc in ("01" + "00" * 30 + "80", "ec" + "ff" * 31):
    check(f"x = 0 with the sign bit set ({enc[:4]}..{enc[-2:]})",
          sigcheck._decompress(bytes.fromhex(enc)), None)
check("identity key as -0, forgery refused",
      _verify(_encode(1, 1), b"any", b_one), False)

# y >= P: the 19 values from P to 2^255 - 1, either sign. 23 of those 38
# name a point once reduced (y = P + 1 is the identity), so only the
# y < P rule keeps each point to one encoding, and keeps the identity
# spelt y = P + 1 from passing as a key.
non_canonical = [(y - sigcheck.P, sign) for y in range(sigcheck.P, 2 ** 255)
                 for sign in (0, 1)]
check("non-canonical encodings that name a point once reduced",
      sum(sigcheck._decompress(_encode(k, sign)) is not None
          for k, sign in non_canonical), 23)
check("non-canonical encodings decoded (as (y - P, sign))",
      [(k, sign) for k, sign in non_canonical
       if sigcheck._decompress(_encode(sigcheck.P + k, sign)) is not None],
      [])
check("identity key as y = P + 1, forgery refused",
      _verify(_encode(sigcheck.P + 1), b"any", b_one), False)

# Not on the curve: for y = 2, x^2 = (y^2 - 1) / (d y^2 + 1) is no square
# mod P (Euler's criterion), so neither root the decoder tries fits.
x2 = 3 * sigcheck._inv(4 * sigcheck.D + 1) % sigcheck.P
check("y = 2 has no x", pow(x2, (sigcheck.P - 1) // 2, sigcheck.P),
      sigcheck.P - 1)
for sign in (0, 1):
    check(f"off-curve y = 2, sign {sign}",
          sigcheck._decompress(_encode(2, sign)), None)
check("off-curve key", _verify(_encode(2), b"", sig1), False)
check("off-curve R", _verify(pk1, b"", _encode(2) + sig1[32:]), False)

# A small-order R with s = k*a: [s]B = [k]A misses R + [k]A by that small
# point alone. The cofactored check RFC 8032 also allows, [8][s]B = [8]R
# + [8][k]A, passes every one; this verifier checks the equation itself,
# so must turn every one away.
a2 = sigcheck._decompress(_pub2)
for enc, order in SMALL_ORDER.items():
    if order == 1:
        continue
    sig = _sign2(b"\x72", big_r=bytes.fromhex(enc))
    s = int.from_bytes(sig[32:], "little")
    k = int.from_bytes(hashlib.sha512(sig[:32] + _pub2 + b"\x72").digest(),
                       "little") % sigcheck.L
    t = sigcheck._decompress(sig[:32])
    if t is not None:     # (a failure to decode is reported above)
        check(f"order-{order} R {enc[:4]}..{enc[-2:]} passes the cofactored"
              " check", sigcheck._equal(
                  sigcheck._mul(8, sigcheck._mul(s, sigcheck.G)),
                  sigcheck._mul(8, sigcheck._add(t, sigcheck._mul(k, a2)))),
              True)
    check(f"order-{order} R {enc[:4]}..{enc[-2:]} refused",
          _verify(_pub2, b"\x72", sig), False)

# Lengths, each built so that only the length check stands in the way:
# RFC test 2's signature ends in a zero byte, so its first 63 bytes carry
# the same s, as does RFC test 1's with a zero byte added; and a key that
# ends in a zero byte (the first seed 0, 1, 2, ... to give one is 74) is
# the same point in 31 bytes or, zero added, 33 - signed over those bytes.
pk2, sig2 = bytes.fromhex(RFC[1][0]), bytes.fromhex(RFC[1][2])
check("RFC test 2's signature ends in a zero byte", sig2[63], 0)
check("signature one byte short", _verify(pk2, b"\x72", sig2[:63]), False)
check("signature one byte long", _verify(pk1, b"", sig1 + b"\x00"), False)
pub74, sign74 = _keypair((74).to_bytes(32, "little"))
check("seed 74's key ends in a zero byte", pub74[31], 0)
for label, key in (("key one byte short", pub74[:31]),
                   ("key one byte long", pub74 + b"\x00")):
    check(label, _verify(key, b"m", sign74(b"m", as_key=key)), False)

# The right length but not bytes: a list of the same ints would get
# through int.from_bytes and bytes() alike. A bytearray is fine.
check("key as a list", _verify(list(pk1), b"", sig1), False)
check("signature as a list", _verify(pk1, b"", list(sig1)), False)
check("signature as text", _verify(pk1, b"", "x" * 64), False)
check("bytearray key and signature",
      _verify(bytearray(pk1), b"", bytearray(sig1)), True)

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
