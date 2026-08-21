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

if fails:
    print("UPDATER TEST FAILED")
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("UPDATER TEST PASSED")
