#!/bin/bash
# build-dmg.sh — package /Applications/Aloe Scribe.app (or $1) into a
# distributable DMG with an Applications-folder drop target.
#
#   bash scripts/build-dmg.sh [path/to/Aloe Scribe.app] [output.dmg]
#
# Signing/notarization happen in release-mac.sh; this only packages.
set -euo pipefail

APP="${1:-/Applications/Aloe Scribe.app}"
OUT="${2:-dist/AloeScribe.dmg}"
[ -d "$APP" ] || { echo "App not found: $APP" >&2; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$(dirname "$OUT")"
rm -f "$OUT"

cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

hdiutil create -volname "Aloe Scribe" -srcfolder "$STAGE" \
    -fs HFS+ -format UDZO -quiet "$OUT"
shasum -a 256 "$OUT" | tee "$OUT.sha256"
echo "DMG ready: $OUT"
