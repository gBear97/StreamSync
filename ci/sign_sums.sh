#!/bin/bash
#
# Sign a release's SHA256SUMS with the release key.
#
#   RELEASE_SIGNING_KEY=<PEM> sign_sums.sh <SHA256SUMS>
#
# Writes <SHA256SUMS>.sig beside it: a raw Ed25519 signature over the
# file's exact bytes, which is what the app's updater checks (sigcheck)
# before it trusts a single checksum. Run from the repository root - it
# imports updater and sigcheck to prove two things before anything is
# uploaded:
#   - the secret is the key the apps were built to trust (a wrong one
#     would make every installed app refuse this release, and silently,
#     until someone tried to update);
#   - the app's own verifier accepts the signature openssl just made.
# The key is written to a private temporary file for openssl and removed
# again on any exit.
#
# PYTHON names the interpreter (python3 by default; Windows runners call
# it python).

set -euo pipefail

SUMS="${1:?SHA256SUMS to sign}"
PY="${PYTHON:-python3}"
if [ -z "${RELEASE_SIGNING_KEY:-}" ]; then
    echo "Missing repository secret: RELEASE_SIGNING_KEY"
    echo "Add it at Settings > Secrets and variables > Actions, with that"
    echo "exact name. Releases are signed, so it is not optional."
    exit 1
fi

umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
printf '%s\n' "$RELEASE_SIGNING_KEY" > "$work/key.pem"

openssl pkeyutl -sign -rawin -inkey "$work/key.pem" \
    -in "$SUMS" -out "$SUMS.sig"
openssl pkey -in "$work/key.pem" -pubout -outform DER -out "$work/pub.der"

"$PY" - "$work/pub.der" "$SUMS" "$SUMS.sig" <<'EOF'
import sys

import sigcheck
import updater

pub = open(sys.argv[1], "rb").read()[-32:]
if pub != updater.RELEASE_KEY:
    raise SystemExit(
        "RELEASE_SIGNING_KEY is not the key the app trusts "
        f"(updater.RELEASE_KEY {updater.RELEASE_KEY.hex()[:16]}..., this "
        f"secret {pub.hex()[:16]}...). Every installed app would refuse "
        "this release - nothing was uploaded.")
sums = open(sys.argv[2], "rb").read()
sig = open(sys.argv[3], "rb").read()
if not sigcheck.verify(updater.RELEASE_KEY, sums, sig):
    raise SystemExit("The app's own verifier rejects this signature.")
print(f"{sys.argv[2]} signed; the app's verifier accepts it.")
EOF
