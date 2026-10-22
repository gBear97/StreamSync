#!/bin/sh
# Build sstap, the helper that records "This Mac's sound" (mac/sstap.swift).
#
#     sh mac/build_sstap.sh [output]      (default: mac/build/sstap)
#
# build_app_mac.sh puts it beside StreamSync inside the app; a source
# checkout finds it in mac/build/. It is built for MIN_MACOS (the app's
# floor, 14.0) and switches the tap on only from 14.2, where macOS has it,
# so the app's every-binary floor check still holds. ARCH picks the CPU
# (default: this Mac's).
set -e
here=$(cd "$(dirname "$0")" && pwd)
out="${1:-$here/build/sstap}"
arch="${ARCH:-$(uname -m)}"
mkdir -p "$(dirname "$out")"
swiftc -O -swift-version 5 -target "$arch-apple-macos${MIN_MACOS:-14.0}" \
    "$here/sstap.swift" -o "$out"
echo "Built $out ($arch, macOS ${MIN_MACOS:-14.0}+)"
