#!/bin/bash
#
# Run a command that has to reach Apple, and run it again when what
# stopped it was the network rather than Apple's answer.
#
#   apple_retry.sh <command> [<argument>...]
#
# Five of the seven releases from 2.0.2 to 2.0.8 needed their failed jobs
# re-run by hand, and not one of those builds had anything wrong with it.
# Stapling fetches the notarization ticket from api.apple-cloudkit.com,
# and `stapler validate` asks CloudKit about it again. On the Intel runner
# that connection dropped - "NSURLErrorDomain Code=-1005 The network
# connection was lost", then stapler's "Error 68" - in 2.0.2, 2.0.3,
# 2.0.5, 2.0.7 and 2.0.8. The re-run of 2.0.2 then lost codesign's request
# to Apple's timestamp server instead ("A timestamp was expected but was
# not found"). Each time, the same command worked on the next run.
#
# On the Intel runner those dropped connections turned out to be
# stapler's HTTP/3 (UDP 443), which GitHub's network there loses;
# ci/block_udp_443.sh now makes stapler use TCP instead. The retries stay
# for any other drop.
#
# So a failure that reads as the network - a dropped or refused
# connection, a timeout, a server error, stapler's Error 68 (EX_NOHOST:
# CloudKit never answered) - is tried again, up to four times in all,
# 30, 60 and 90 s apart. Anything else is an answer: no ticket (stapler's
# Error 65), a signature Apple won't take, a file that isn't there. That
# still fails at once, with the command's own words and exit status;
# waiting would not change the answer, only delay it.
#
# Each try's output is printed when the command finishes: a try that will
# be repeated goes to stderr, the last one to stdout. A caller that reads
# stdout - notarize.sh reads notarytool's JSON - then gets only the answer
# that counted, and the log still shows every try.
#
# APPLE_RETRY_PAUSE overrides the first pause (30 s); the later ones are
# two and three times as long.

set -uo pipefail

if [ "$#" -eq 0 ]; then
    echo "usage: apple_retry.sh <command> [<argument>...]" >&2
    exit 2
fi

TRIES=4
PAUSE="${APPLE_RETRY_PAUSE:-30}"

# What a lost connection looks like from codesign, stapler and notarytool.
# Only the output of a failed try is held up against these. The NSURLError
# codes are a timeout, no such host, no connection, a dropped connection,
# a failed DNS lookup and no network at all, in either way they get
# written ("Code=-1005" or "NSURLErrorDomain error -1005.").
NETWORK='NSURLErrorDomain( Code=| error )-10(01|03|04|05|06|09)([^0-9]|$)'
NETWORK+='|action failed! Error 68\.'
NETWORK+='|The network connection was lost'
NETWORK+='|The request timed out'
NETWORK+='|Could not connect to the server'
NETWORK+='|A server with the specified hostname could not be found'
NETWORK+='|The Internet connection appears to be offline'
NETWORK+='|Connection reset by peer'
NETWORK+='|deadlineExceeded'
NETWORK+='|HTTP status code: 5[0-9][0-9]'
NETWORK+='|A timestamp was expected but was not found'
NETWORK+='|The timestamp service is not available'

# "stapler" reads better than "xcrun" in a log line.
tool=$1
[ "$tool" = xcrun ] && tool=${2:-xcrun}

for try in $(seq 1 "$TRIES"); do
    out=$("$@" 2>&1)
    status=$?
    if [ "$status" -eq 0 ] || ! grep -qE "$NETWORK" <<< "$out"; then
        [ -n "$out" ] && printf '%s\n' "$out"
        exit "$status"
    fi
    if [ "$try" -eq "$TRIES" ]; then
        [ -n "$out" ] && printf '%s\n' "$out"
        echo "$tool could not reach Apple in $TRIES tries (exit $status)." >&2
        echo "Nothing here says the build is bad; re-run the failed jobs." >&2
        exit "$status"
    fi
    printf '%s\n' "$out" >&2
    pause=$(( PAUSE * try ))
    echo "$tool lost its connection to Apple (try $try of $TRIES);" \
         "trying again in $pause s." >&2
    sleep "$pause"
done
