#!/bin/bash
# build-app.sh — Build the macOS .app bundle.
#
# Compiles the Swift helper (with stable ad-hoc signing), then runs py2app,
# then re-signs the .app bundle with a stable identifier and installs it to
# /Applications. The stable identifiers keep TCC happy across rebuilds.
#
# Usage: bash scripts/build-app.sh

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

APP_IDENTIFIER="com.aloescribe.app"
APP_NAME="Aloe Scribe"

# 1. Compile + sign the Swift helper.
bash scripts/build-helper.sh

# 2. Build the .app bundle via py2app.
echo "Quitting any running instance..."
osascript -e "tell application \"$APP_NAME\" to quit" >/dev/null 2>&1 || true
sleep 1

echo "Cleaning previous build..."
rm -rf build dist

echo "Running py2app (this takes ~30s)..."
.venv/bin/python3 setup.py py2app 2>&1 | tail -3

# 3. Re-sign the .app bundle with the stable identifier. The helper inside
#    keeps its own signature (already stable). We don't use --deep so we don't
#    overwrite the helper's identifier.
echo "Signing .app bundle ($APP_IDENTIFIER)..."
codesign --force --sign - --identifier "$APP_IDENTIFIER" "dist/${APP_NAME}.app"

# 4. Install to /Applications.
echo "Installing to /Applications/${APP_NAME}.app..."
rm -rf "/Applications/${APP_NAME}.app"
cp -R "dist/${APP_NAME}.app" "/Applications/${APP_NAME}.app"

echo ""
echo "Done."
codesign -dv "/Applications/${APP_NAME}.app" 2>&1 | grep -E "Identifier|Signature"
codesign -dv "/Applications/${APP_NAME}.app/Contents/Resources/bin/aloe-audio-capture" 2>&1 | grep -E "Identifier|Signature"
