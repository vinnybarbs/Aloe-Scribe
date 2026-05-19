#!/bin/bash
# build-app.sh — Build the macOS .app bundle for Aloe Scribe.
#
# We use a *launcher* bundle (not py2app): the .app is a minimal wrapper
# whose binary just exec's the project's venv Python on src/main.py. py2app
# breaks on mlx's namespace-package layout, and launching from the venv keeps
# the install simple (no frozen-Python landmines, native libs Just Work).
#
# Side effects:
#   - Compiles bin/aloe-audio-capture via scripts/build-helper.sh
#   - Code-signs both the helper and the .app with stable ad-hoc identifiers
#     so macOS TCC remembers Screen Recording / Microphone grants across
#     rebuilds.
#   - Installs the bundle to /Applications/Aloe Scribe.app
#
# Usage: bash scripts/build-app.sh

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

APP_NAME="Aloe Scribe"
APP_IDENTIFIER="com.aloescribe.app"
APP_DIR="$PROJECT_DIR/dist/${APP_NAME}.app"
CONTENTS="$APP_DIR/Contents"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python3"

if [ ! -x "$VENV_PYTHON" ]; then
    echo "Error: $VENV_PYTHON not found." >&2
    echo "Run scripts/install-mac.sh first to create the venv." >&2
    exit 1
fi

# 1. Compile + sign the Swift helper.
bash scripts/build-helper.sh

# 2. Quit any running instance.
echo "Quitting any running instance..."
osascript -e "tell application \"${APP_NAME}\" to quit" >/dev/null 2>&1 || true
sleep 1

# 3. Build the .app bundle from scratch.
echo "Building ${APP_NAME}.app..."
rm -rf "$APP_DIR"
mkdir -p "$CONTENTS/MacOS"
mkdir -p "$CONTENTS/Resources"

# Launcher script (the bundle's executable). It exec's the venv Python so the
# main process is still ".app-context" for TCC purposes.
cat > "$CONTENTS/MacOS/aloe-scribe" << LAUNCHER
#!/bin/bash
export PROJECT_DIR="$PROJECT_DIR"
cd "$PROJECT_DIR"
exec "$VENV_PYTHON" "$PROJECT_DIR/src/main.py"
LAUNCHER
chmod +x "$CONTENTS/MacOS/aloe-scribe"

# Info.plist — includes both microphone + screen-capture usage strings so the
# first-run TCC prompts have something descriptive.
cat > "$CONTENTS/Info.plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>${APP_NAME}</string>
    <key>CFBundleDisplayName</key>
    <string>${APP_NAME}</string>
    <key>CFBundleIdentifier</key>
    <string>${APP_IDENTIFIER}</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleExecutable</key>
    <string>aloe-scribe</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>Aloe Scribe needs microphone access to record meeting audio.</string>
    <key>NSScreenCaptureUsageDescription</key>
    <string>Aloe Scribe needs screen capture access to record system audio (the audio from the apps you're listening to) — no video is captured or saved.</string>
    <key>LSUIElement</key>
    <false/>
</dict>
</plist>
EOF

# App icon — convert assets/icon.png to .icns via sips/iconutil.
ICON_SRC="$PROJECT_DIR/assets/icon.png"
if [ -f "$ICON_SRC" ] && command -v sips &>/dev/null; then
    ICONSET="$CONTENTS/Resources/AppIcon.iconset"
    mkdir -p "$ICONSET"
    for SIZE in 16 32 64 128 256 512; do
        sips -z $SIZE $SIZE "$ICON_SRC" --out "$ICONSET/icon_${SIZE}x${SIZE}.png" &>/dev/null
        DOUBLE=$((SIZE * 2))
        if [ $DOUBLE -le 1024 ]; then
            sips -z $DOUBLE $DOUBLE "$ICON_SRC" --out "$ICONSET/icon_${SIZE}x${SIZE}@2x.png" &>/dev/null
        fi
    done
    iconutil -c icns "$ICONSET" -o "$CONTENTS/Resources/AppIcon.icns" 2>/dev/null && rm -rf "$ICONSET"
fi

# 4. Sign the .app bundle with a stable identifier so TCC remembers grants.
echo "Signing .app bundle (${APP_IDENTIFIER})..."
codesign --force --sign - --identifier "${APP_IDENTIFIER}" "$APP_DIR"

# 5. Install to /Applications.
echo "Installing to /Applications/${APP_NAME}.app..."
rm -rf "/Applications/${APP_NAME}.app"
cp -R "$APP_DIR" "/Applications/${APP_NAME}.app"

echo ""
echo "Done. Open from Spotlight or /Applications."
codesign -dv "/Applications/${APP_NAME}.app" 2>&1 | grep -E "Identifier|Signature"
codesign -dv "$PROJECT_DIR/bin/aloe-audio-capture" 2>&1 | grep -E "Identifier|Signature"
