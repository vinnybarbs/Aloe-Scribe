#!/bin/bash
# Build Aloe Scribe.app — a macOS .app bundle that launches the Python app
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="Aloe Scribe"
APP_DIR="$PROJECT_DIR/dist/${APP_NAME}.app"
CONTENTS="$APP_DIR/Contents"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python3"

echo "Building ${APP_NAME}.app..."

# Clean previous build
rm -rf "$APP_DIR"

# Create .app structure
mkdir -p "$CONTENTS/MacOS"
mkdir -p "$CONTENTS/Resources"

# --- Launcher script ---
cat > "$CONTENTS/MacOS/aloe-scribe" << EOF
#!/bin/bash
cd "$PROJECT_DIR"
exec "$VENV_PYTHON" "$PROJECT_DIR/src/main.py"
EOF
chmod +x "$CONTENTS/MacOS/aloe-scribe"

# --- Info.plist ---
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
    <string>com.aloescribe.app</string>
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
    <string>10.15</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>Aloe Scribe needs microphone access to record meeting audio.</string>
    <key>LSUIElement</key>
    <false/>
</dict>
</plist>
EOF

# --- App icon ---
# Convert icon.png to .icns
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
    echo "  App icon created"
else
    echo "  Warning: Could not create .icns icon"
fi

echo ""
echo "Built: $APP_DIR"
echo ""

# Copy to Applications
if [ -d "/Applications" ]; then
    cp -R "$APP_DIR" "/Applications/${APP_NAME}.app"
    echo "Installed to /Applications/${APP_NAME}.app"
    echo ""
    echo "You can now:"
    echo "  - Find 'Aloe Scribe' in Spotlight (Cmd+Space)"
    echo "  - Open it from /Applications"
    echo "  - Drag it to your Dock"
fi
