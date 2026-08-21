"""Checks the update logic that can be exercised without a Mac.

Covers version comparison, picking the right architecture's asset, and
the checksum lookup - the parts that decide whether an update happens at
all. Mounting and swapping a bundle needs real macOS and is not tested
here.

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
check("arm64", updater.pick_asset(ASSETS, "arm64")["name"],
      "StreamSync-1.1.0-arm64-abc1234.dmg")
check("x86_64", updater.pick_asset(ASSETS, "x86_64")["name"],
      "StreamSync-1.1.0-x86_64-abc1234.dmg")

try:
    updater.pick_asset([{"name": "notes.txt"}], "arm64")
    fails.append("missing asset: should have raised UpdateError")
except updater.UpdateError:
    pass

# x86_64 must not be matched by an arm64 lookup, and vice versa.
try:
    updater.pick_asset([{"name": "StreamSync-1.1.0-x86_64-abc.dmg"}], "arm64")
    fails.append("wrong arch: should have raised rather than cross-match")
except updater.UpdateError:
    pass

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

if fails:
    print("UPDATER TEST FAILED")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("UPDATER TEST PASSED")
