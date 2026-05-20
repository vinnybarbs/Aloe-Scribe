#!/bin/bash
# build-app.sh — Build the macOS .app bundle for Aloe Scribe via py2app.
#
# We use py2app (not a launcher .app) because Tahoe's NSStatusItem filter +
# TCC attribution only treat the .app properly when the bundle has a "real
# app" structure — bundled Python framework, lib/python3.12 trees, py2app's
# native launcher, the lot. Launcher .apps (bash or Swift exec'ing a venv
# Python) end up with a separate `aloe-python` TCC entity AND a hidden
# tray icon. py2app builds avoid both.
#
# mlx + parakeet_mlx + huggingface_hub are explicitly *excluded* from the
# frozen bundle (mlx's namespace-package layout breaks py2app's modulegraph).
# main.py prepends the dev venv's site-packages to sys.path at runtime so
# those imports resolve from there.
#
# Side effects:
#   - Compiles bin/aloe-audio-capture via scripts/build-helper.sh
#   - Runs py2app, producing dist/Aloe Scribe.app
#   - Code-signs the bundled helper + .app with stable ad-hoc identifiers
#     (com.aloescribe.audio-capture + com.aloescribe.app)
#   - Installs to /Applications/Aloe Scribe.app
#
# Usage: bash scripts/build-app.sh

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

APP_NAME="Aloe Scribe"
APP_IDENTIFIER="com.aloescribe.app"
HELPER_IDENTIFIER="com.aloescribe.audio-capture"
APP_DIR="$PROJECT_DIR/dist/${APP_NAME}.app"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python3"

if [ ! -x "$VENV_PYTHON" ]; then
    echo "Error: $VENV_PYTHON not found. Run scripts/install-mac.sh first." >&2
    exit 1
fi

# 1. Compile + sign the Swift audio-capture helper (committed copy in bin/
#    gets refreshed). py2app picks it up via DATA_FILES in setup.py and
#    drops it at Contents/Resources/bin/aloe-audio-capture inside the .app.
bash scripts/build-helper.sh

# 2. Quit any running instance and clean previous build artifacts.
echo "Quitting any running instance..."
osascript -e "tell application \"${APP_NAME}\" to quit" >/dev/null 2>&1 || true
sleep 1
pkill -f "$APP_DIR/Contents/MacOS" 2>/dev/null || true
rm -rf "$PROJECT_DIR/build" "$PROJECT_DIR/dist"

# 3. Run py2app.
echo "Running py2app (this takes ~30–60s)..."
"$VENV_PYTHON" setup.py py2app 2>&1 | tail -5

if [ ! -d "$APP_DIR" ]; then
    echo "py2app build failed — $APP_DIR not produced." >&2
    exit 1
fi

# 4. Code-sign the helper (inside the bundle) with its stable identifier,
#    then the bundle with com.aloescribe.app. We do NOT use --deep so the
#    helper keeps its own identifier — but both live under the same .app
#    so TCC treats them as one Aloe Scribe identity at the bundle level.
HELPER_IN_BUNDLE="$APP_DIR/Contents/Resources/bin/aloe-audio-capture"
if [ -f "$HELPER_IN_BUNDLE" ]; then
    echo "Signing bundled helper (${HELPER_IDENTIFIER})..."
    chmod +x "$HELPER_IN_BUNDLE"
    codesign --force --sign - --identifier "${HELPER_IDENTIFIER}" "$HELPER_IN_BUNDLE"
else
    echo "Warning: bundled helper not found at $HELPER_IN_BUNDLE" >&2
fi

echo "Signing .app bundle (${APP_IDENTIFIER})..."
codesign --force --sign - --identifier "${APP_IDENTIFIER}" "$APP_DIR"

# 5. Install to /Applications.
echo "Installing to /Applications/${APP_NAME}.app..."
rm -rf "/Applications/${APP_NAME}.app"
cp -R "$APP_DIR" "/Applications/${APP_NAME}.app"

echo ""
echo "Done. Open from Spotlight or /Applications."
codesign -dv "/Applications/${APP_NAME}.app" 2>&1 | grep -E "Identifier|Signature"
codesign -dv "/Applications/${APP_NAME}.app/Contents/Resources/bin/aloe-audio-capture" 2>&1 | grep -E "Identifier|Signature" || true
