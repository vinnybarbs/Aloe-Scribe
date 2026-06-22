#!/bin/bash
# build-helper.sh — Compile the Swift audio-capture helper and code-sign it
# with a stable ad-hoc identifier so macOS TCC remembers granted permissions
# across rebuilds.
#
# Usage: bash scripts/build-helper.sh

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

SRC="tools/aloe-audio-capture/main.swift"
OUT="bin/aloe-audio-capture"
IDENTIFIER="com.aloescribe.audio-capture"

mkdir -p bin

echo "Compiling Swift helper..."
swiftc -O \
  -target arm64-apple-macos13.0 \
  -framework ScreenCaptureKit \
  -framework AVFoundation \
  -framework CoreMedia \
  -framework Foundation \
  "$SRC" \
  -o "$OUT"

# Sign with a stable self-signed identity if one exists, so macOS keeps granted
# TCC permissions (Screen Recording / Mic) across rebuilds. Falls back to ad-hoc
# ("-") otherwise — which works, but resets permissions on every rebuild.
#   Create the identity once: scripts/create-signing-cert.sh
SIGN_ID="${ALOE_SIGN_ID:-}"
if [ -z "$SIGN_ID" ]; then
    if security find-identity -p codesigning 2>/dev/null | grep -q "Aloe Scribe Local Signing"; then
        SIGN_ID="Aloe Scribe Local Signing"
    else
        SIGN_ID="-"
    fi
fi
echo "Code-signing helper (identity: $SIGN_ID, id: $IDENTIFIER)..."
codesign --force --sign "$SIGN_ID" --identifier "$IDENTIFIER" "$OUT"

echo "Built: $OUT"
codesign -dv "$OUT" 2>&1 | grep -E "Identifier|Signature"
