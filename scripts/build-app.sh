#!/bin/bash
# build-app.sh — Build the macOS .app bundle for Aloe Scribe.
#
# We copy the homebrew Python binary into the bundle (not symlink) so that
# macOS's NSBundle.mainBundle() resolves to Aloe Scribe.app — not Python.app.
# Without that the application menu shows "Python", the tray icon doesn't
# render with the bundle's identity, and Screen Recording TCC prompts get
# misattributed.
#
# Stdlib stays in the homebrew framework via PYTHONHOME; venv site-packages
# come in via PYTHONPATH. The launcher uses `exec` so the python process
# replaces the shell — no extra bash hanging around.
#
# Side effects:
#   - Compiles bin/aloe-audio-capture via scripts/build-helper.sh
#   - Code-signs python copy + helper + .app with stable ad-hoc identifiers
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
VENV_DIR="$PROJECT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python3"

if [ ! -x "$VENV_PYTHON" ]; then
    echo "Error: $VENV_PYTHON not found." >&2
    echo "Run scripts/install-mac.sh first to create the venv." >&2
    exit 1
fi

# Resolve the real Python binary the venv points at, and the framework prefix
# so we can set PYTHONHOME at launch time.
#
# Important: homebrew's Frameworks/.../bin/python3.12 is a *stub* that
# re-execs the framework's Resources/Python.app/Contents/MacOS/Python.
# That re-exec is what put us back inside Python.app's bundle context (wrong
# menu name, broken tray). We grab the actual interpreter instead — same
# binary, no re-exec.
REAL_PYTHON_STUB="$(readlink -f "$VENV_PYTHON")"
PYTHON_HOME="$(dirname "$(dirname "$REAL_PYTHON_STUB")")"   # …/Versions/3.12
REAL_PYTHON="$PYTHON_HOME/Resources/Python.app/Contents/MacOS/Python"
if [ ! -x "$REAL_PYTHON" ]; then
    echo "Error: Could not find framework Python at $REAL_PYTHON" >&2
    echo "(falling back to stub at $REAL_PYTHON_STUB)" >&2
    REAL_PYTHON="$REAL_PYTHON_STUB"
fi
PYTHON_MINOR="$("$REAL_PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
VENV_SITE="$VENV_DIR/lib/python${PYTHON_MINOR}/site-packages"

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

# Copy the real Python binary into the bundle so its realpath is inside
# Aloe Scribe.app. The binary loads libpython3.12.dylib via an absolute
# path, so no rpath surgery is needed.
echo "Copying Python binary into bundle..."
cp -L "$REAL_PYTHON" "$CONTENTS/MacOS/aloe-python"
chmod +x "$CONTENTS/MacOS/aloe-python"

# Launcher (the bundle's CFBundleExecutable). Sets PYTHONHOME so the copied
# Python finds the homebrew framework's stdlib, and PYTHONPATH so it picks
# up the venv's site-packages (PyQt6, parakeet-mlx, mlx, etc.).
cat > "$CONTENTS/MacOS/aloe-scribe" << LAUNCHER
#!/bin/bash
export PYTHONHOME="$PYTHON_HOME"
export PYTHONPATH="$VENV_SITE"
export ALOE_SCRIBE_PROJECT_DIR="$PROJECT_DIR"
cd "$PROJECT_DIR"
exec "\$(dirname "\$0")/aloe-python" "$PROJECT_DIR/src/main.py"
LAUNCHER
chmod +x "$CONTENTS/MacOS/aloe-scribe"

# Info.plist. CFBundleName + DisplayName drive the application menu title.
# CFBundleExecutable points at the launcher script, but because the launcher
# exec's aloe-python (the copied Python that lives inside this bundle),
# NSBundle.mainBundle() still resolves to Aloe Scribe.app.
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

# 4. Code-sign the embedded python with a stable identifier first, then the
#    bundle. Using --deep would clobber the helper's identifier, so we sign
#    each piece by hand.
echo "Signing embedded python..."
codesign --force --sign - --identifier com.aloescribe.python "$CONTENTS/MacOS/aloe-python"

echo "Signing .app bundle (${APP_IDENTIFIER})..."
codesign --force --sign - --identifier "${APP_IDENTIFIER}" "$APP_DIR"

# 5. Install to /Applications.
echo "Installing to /Applications/${APP_NAME}.app..."
rm -rf "/Applications/${APP_NAME}.app"
cp -R "$APP_DIR" "/Applications/${APP_NAME}.app"

echo ""
echo "Done. Open from Spotlight or /Applications."
codesign -dv "/Applications/${APP_NAME}.app" 2>&1 | grep -E "Identifier|Signature"
codesign -dv "/Applications/${APP_NAME}.app/Contents/MacOS/aloe-python" 2>&1 | grep -E "Identifier|Signature"
codesign -dv "$PROJECT_DIR/bin/aloe-audio-capture" 2>&1 | grep -E "Identifier|Signature"
