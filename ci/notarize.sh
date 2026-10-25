#!/bin/bash
#
# Notarize one artifact with Apple and staple the ticket to it.
#
#   notarize.sh <artifact to submit> <thing to staple>
#
# This replaced `notarytool submit --wait --timeout 45m`, which lost three
# release runs in a row. Every one of those submissions was accepted by
# Apple - the account's submission history shows five accepted and no
# refusals since the signing fix - but the wait expired before Apple
# answered, the step failed, and the release went with it. The submission
# was never the fragile part. A single fixed-length blocking wait was.
#
# So: submit first and hold on to the id, then poll for as long as we are
# given. A slow answer then costs time instead of costing the release, and
# whatever happens the id is printed, so a submission that outlives the
# budget can still be asked about afterwards rather than being abandoned.
#
# Each call here that reaches Apple, the polling aside, goes through
# apple_retry.sh: a dropped connection is tried again, and anything else
# fails at once. The stapling at the end needed it most - it lost four
# Intel builds after Apple had already accepted them. The polling needs
# no such help, since a poll that gets no status is simply asked again
# 30 s later until the budget runs out; it does print why a poll got no
# answer, so one that keeps failing for another reason is not taken for a
# slow queue.
#
# Needs NOTARY_KEY_FILE, NOTARY_KEY_ID and NOTARY_ISSUER_ID in the
# environment. NOTARY_BUDGET_SECONDS overrides how long to wait.

set -uo pipefail

SUBMIT_PATH="${1:?what to submit}"
STAPLE_PATH="${2:?what to staple}"
BUDGET="${NOTARY_BUDGET_SECONDS:-4500}"   # 75 minutes
RETRY="$(dirname "$0")/apple_retry.sh"

AUTH=(--key "$NOTARY_KEY_FILE"
      --key-id "$NOTARY_KEY_ID"
      --issuer "$NOTARY_ISSUER_ID")

# notarytool's JSON is the contract; its human output reflows between
# Xcode versions and has already caused one misread status.
field() {
    python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get(sys.argv[1], ""))
except Exception:
    print("")' "$1" 2>/dev/null
}

# No 2>&1: apple_retry.sh already folds notarytool's stderr into what it
# prints, and keeps its own notes on stderr, out of the JSON.
out=$("$RETRY" xcrun notarytool submit "$SUBMIT_PATH" "${AUTH[@]}" \
        --output-format json)
ID=$(printf '%s' "$out" | field id)
if [ -z "$ID" ]; then
    echo "Could not submit $(basename "$SUBMIT_PATH"). notarytool said:"
    printf '%s\n' "$out"
    exit 1
fi
echo "Submitted $(basename "$SUBMIT_PATH") - Apple calls it $ID"

ERR=$(mktemp)
trap 'rm -f "$ERR"' EXIT

deadline=$(( $(date +%s) + BUDGET ))
while :; do
    status=$(xcrun notarytool info "$ID" "${AUTH[@]}" \
               --output-format json 2>"$ERR" | field status)

    case "$status" in
        Accepted)
            echo "Apple accepted $ID"
            break
            ;;
        Invalid|Rejected)
            echo ""
            echo "=== Apple refused this build. Its reasons: ==="
            # The reasons live only in the log; without it a refusal is
            # just a word, which is how the first one went unexplained.
            "$RETRY" xcrun notarytool log "$ID" "${AUTH[@]}" \
                || echo "(the log could not be fetched)"
            exit 1
            ;;
    esac

    left=$(( deadline - $(date +%s) ))
    if [ "$left" -le 0 ]; then
        echo ""
        echo "=== Apple has not answered after $(( BUDGET / 60 )) minutes. ==="
        echo "Nothing here says the build is bad, and the submission is"
        echo "still queued rather than lost - every submission this"
        echo "pipeline has made since the signing fix was accepted in the"
        echo "end. Re-run, or ask about this one directly:"
        echo "  xcrun notarytool info $ID --key ... --key-id ... --issuer ..."
        exit 1
    fi
    if [ -n "$status" ]; then
        echo "  $status - $(( left / 60 )) min of budget left"
    else
        # No answer this time, usually a dropped connection: it costs one
        # poll, not the release. notarytool's last line says what it was.
        why=$(tail -n 1 "$ERR" | cut -c1-200)
        echo "  no answer${why:+ ($why)} - $(( left / 60 )) min of budget left"
    fi
    sleep 30
done

# Both ask CloudKit for the ticket, over the connection that kept
# dropping on the Intel runner. A staple that still fails stops here, with
# its own reason, instead of validate's "does not have a ticket stapled".
"$RETRY" xcrun stapler staple "$STAPLE_PATH" || exit
"$RETRY" xcrun stapler validate "$STAPLE_PATH"
