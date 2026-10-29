#!/bin/bash
#
# Refuse outgoing UDP to port 443 for the rest of this job, so stapler
# reaches Apple over TCP.
#
#   ci/block_udp_443.sh            load the block and check that pf took it
#   ci/block_udp_443.sh --report   print how much it has turned away
#
# Since about 2026-10-12, GitHub's macos-15-intel runners have dropped
# the HTTP/3 connection `xcrun stapler` opens to Apple's ticket lookup at
# api.apple-cloudkit.com. After about 7 s it fails with NSURLErrorDomain
# -1005 ("The network connection was lost"), then stapler's "Error 68".
# HTTP/3 runs over UDP port 443, and HTTPS over TCP works on the same
# runners, which is why notarytool never failed there. GitHub reproduced
# it and gave this pf rule as the workaround, in
# https://github.com/actions/runner-images/issues/14924 - with UDP 443
# refused, stapler falls back to TCP. The Intel build hit this in 2.0.2,
# 2.0.3, 2.0.5, 2.0.7, 2.0.8 and 2.0.9; for 2.0.9, all four of
# ci/apple_retry.sh's tries over three minutes failed too. Apple Silicon
# runners are not affected and are left alone.
#
# The rule replaces the runner's own pf rules, which nothing in a build
# needs, and lasts until the runner is torn down after the job. "return"
# answers each packet with "port unreachable", so stapler gives up on
# HTTP/3 at once instead of waiting it out. ci/apple_retry.sh stays
# around every Apple call for any other dropped connection.

set -euo pipefail

if [ "${1:-}" = "--report" ]; then
    sudo pfctl -v -s rules
    exit 0
fi

echo "block return out quick proto udp from any to any port 443" | sudo pfctl -f -
sudo pfctl -E

# What pf holds now, not what it was asked to load. It prints the rule
# back as "... port = 443".
rules=$(sudo pfctl -s rules)
info=$(sudo pfctl -s info)
if ! grep -Eq '^block return out quick proto udp from any to any port (= )?443' <<< "$rules" \
        || ! grep -q '^Status: Enabled' <<< "$info"; then
    echo "pf did not take the UDP 443 block. What it holds:" >&2
    printf '%s\n' "$rules" "$(head -1 <<< "$info")" >&2
    exit 1
fi
echo "Outgoing UDP to port 443 is refused for the rest of this job, so stapler reaches Apple over TCP."
