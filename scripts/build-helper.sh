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

echo "Code-signing with stable identifier ($IDENTIFIER)..."
codesign --force --sign - --identifier "$IDENTIFIER" "$OUT"

echo "Built: $OUT"
codesign -dv "$OUT" 2>&1 | grep -E "Identifier|Signature"
