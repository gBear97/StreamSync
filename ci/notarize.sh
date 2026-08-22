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
# Needs NOTARY_KEY_FILE, NOTARY_KEY_ID and NOTARY_ISSUER_ID in the
# environment. NOTARY_BUDGET_SECONDS overrides how long to wait.

set -uo pipefail

SUBMIT_PATH="${1:?what to submit}"
STAPLE_PATH="${2:?what to staple}"
BUDGET="${NOTARY_BUDGET_SECONDS:-4500}"   # 75 minutes

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

out=$(xcrun notarytool submit "$SUBMIT_PATH" "${AUTH[@]}" \
        --output-format json 2>&1)
ID=$(printf '%s' "$out" | field id)
if [ -z "$ID" ]; then
    echo "Could not submit $(basename "$SUBMIT_PATH"). notarytool said:"
    printf '%s\n' "$out"
    exit 1
fi
echo "Submitted $(basename "$SUBMIT_PATH") - Apple calls it $ID"

deadline=$(( $(date +%s) + BUDGET ))
while :; do
    status=$(xcrun notarytool info "$ID" "${AUTH[@]}" \
               --output-format json 2>/dev/null | field status)

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
            xcrun notarytool log "$ID" "${AUTH[@]}" \
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
    echo "  ${status:-no status yet} - $(( left / 60 )) min of budget left"
    sleep 30
done

xcrun stapler staple "$STAPLE_PATH"
xcrun stapler validate "$STAPLE_PATH"
