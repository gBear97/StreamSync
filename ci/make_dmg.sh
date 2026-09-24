#!/bin/bash
#
# Package an app bundle as a compressed disk image.
#
#   make_dmg.sh <app bundle> <volume name> <disk image to write>
#
# hdiutil create sometimes fails on GitHub's macOS runners with
# "hdiutil: create failed - Resource busy": something else on the machine
# briefly has hold of the new image while it is being made. The app itself
# is fine - the same bundle packages on the next try - but the failure
# took the whole build with it, after the app had built and passed its
# self-check. So a busy device is retried a few times, with a pause to
# let whatever held it let go; any other error fails at once, as before.

set -uo pipefail

APP="${1:?app bundle}"
VOLNAME="${2:?volume name}"
DMG="${3:?disk image to write}"
TRIES=5

for try in $(seq 1 "$TRIES"); do
    out=$(hdiutil create -volname "$VOLNAME" -srcfolder "$APP" \
          -fs HFS+ -ov -format UDZO "$DMG" 2>&1)
    status=$?
    echo "$out"
    [ "$status" -eq 0 ] && exit 0
    case "$out" in
        *"Resource busy"*) ;;
        *) exit "$status" ;;
    esac
    if [ "$try" -lt "$TRIES" ]; then
        echo "hdiutil: device busy (try $try of $TRIES), retrying in $((try * 10)) s"
        sleep $((try * 10))
    fi
done
echo "hdiutil: still busy after $TRIES tries"
exit 1
