#!/bin/bash
# Aloe Scribe — macOS Install Script
# Run: bash scripts/install-mac.sh

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
BOLD='\033[1m'

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
WHISPER_DIR="$HOME/whisper.cpp"

echo ""
echo "${BOLD}=========================================${NC}"
echo "${BOLD}  Aloe Scribe — macOS Setup${NC}"
echo "${BOLD}=========================================${NC}"
echo ""

# -----------------------------------------------------------
# 1. Homebrew
# -----------------------------------------------------------
echo -e "${GREEN}[1/7]${NC} Checking Homebrew..."
if ! command -v brew &>/dev/null; then
    echo "Homebrew not found. Installing..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
echo "  Homebrew OK"

# -----------------------------------------------------------
# 2. System dependencies
# -----------------------------------------------------------
echo -e "${GREEN}[2/7]${NC} Installing system dependencies..."
brew install ffmpeg rclone python@3.12 cmake git 2>/dev/null || true
echo "  ffmpeg, rclone, python, cmake OK"

# -----------------------------------------------------------
# 3. BlackHole (optional — system audio capture)
# -----------------------------------------------------------
echo -e "${GREEN}[3/7]${NC} BlackHole (optional)..."
if brew list --cask blackhole-2ch &>/dev/null 2>&1; then
    echo "  BlackHole installed — system audio capture available"
else
    echo "  BlackHole not installed — recording mic only (fine for most meetings)"
    echo "  Your mic picks up your voice directly and call audio from speakers."
fi

# -----------------------------------------------------------
# 4. Python venv + dependencies
# -----------------------------------------------------------
echo -e "${GREEN}[4/7]${NC} Setting up Python environment..."
# Always recreate venv to avoid stale cached builds
rm -rf "$VENV_DIR"
# Use Homebrew's python3.12 for the venv (has tomllib built-in)
BREW_PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
if [ -x "$BREW_PYTHON" ]; then
    "$BREW_PYTHON" -m venv "$VENV_DIR"
    echo "  Using Python 3.12 from Homebrew"
else
    python3 -m venv "$VENV_DIR"
    echo "  Using system python3"
fi
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -q \
    "PyQt6>=6.6.0" \
    "pyobjc-framework-Cocoa>=10.0" \
    "pillow>=10.0.0" \
    "tomli>=2.0.0" \
    "py2app>=0.28.0"
echo "  Python venv OK ($(${VENV_DIR}/bin/python3 --version))"

# -----------------------------------------------------------
# 5. whisper.cpp with Metal
# -----------------------------------------------------------
echo -e "${GREEN}[5/7]${NC} Building whisper.cpp with Metal acceleration..."
if [ -d "$WHISPER_DIR" ]; then
    echo "  whisper.cpp directory exists, pulling latest..."
    cd "$WHISPER_DIR" && git pull -q
else
    git clone https://github.com/ggerganov/whisper.cpp.git "$WHISPER_DIR"
    cd "$WHISPER_DIR"
fi

cmake -B build -DWHISPER_METAL=ON -DCMAKE_BUILD_TYPE=Release 2>/dev/null
cmake --build build --config Release -j$(sysctl -n hw.ncpu) 2>/dev/null
echo "  whisper.cpp built with Metal"

# Download model — large-v3-turbo is the recommended default (near-large
# accuracy at ~3-4x realtime; ~1.6 GB)
MODEL="large-v3-turbo"
MODEL_PATH="$WHISPER_DIR/models/ggml-${MODEL}.bin"
if [ -f "$MODEL_PATH" ]; then
    echo "  Model '$MODEL' already downloaded"
else
    echo "  Downloading Whisper '$MODEL' model..."
    bash "$WHISPER_DIR/models/download-ggml-model.sh" "$MODEL"
fi
echo "  Model OK: $MODEL_PATH"

# -----------------------------------------------------------
# 6. Update config
# -----------------------------------------------------------
echo -e "${GREEN}[6/7]${NC} Updating config..."
cd "$PROJECT_DIR"
CONFIG="$PROJECT_DIR/config/config.toml"

sed -i '' "s|binary_path = .*|binary_path = \"$WHISPER_DIR/build/bin/whisper-cli\"|" "$CONFIG"
sed -i '' "s|model_path = .*|model_path = \"$MODEL_PATH\"|" "$CONFIG"
echo "  Config updated with whisper paths"

# -----------------------------------------------------------
# 7. Build native .app bundle with py2app
# -----------------------------------------------------------
echo -e "${GREEN}[7/7]${NC} Building Aloe Scribe.app..."

cd "$PROJECT_DIR"

# Clean ALL previous builds and caches
rm -rf build dist *.egg-info .eggs
rm -rf /Applications/Aloe\ Scribe.app
find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# Generate .icns icon from PNG
ICON_SRC="$PROJECT_DIR/assets/icon.png"
ICNS_OUT="$PROJECT_DIR/assets/AppIcon.icns"
if [ -f "$ICON_SRC" ] && command -v sips &>/dev/null && [ ! -f "$ICNS_OUT" ]; then
    echo "  Generating .icns icon..."
    ICONSET="/tmp/AloeScribe.iconset"
    rm -rf "$ICONSET" && mkdir -p "$ICONSET"
    for SIZE in 16 32 128 256 512; do
        sips -z $SIZE $SIZE "$ICON_SRC" --out "$ICONSET/icon_${SIZE}x${SIZE}.png" &>/dev/null
        DOUBLE=$((SIZE * 2))
        sips -z $DOUBLE $DOUBLE "$ICON_SRC" --out "$ICONSET/icon_${SIZE}x${SIZE}@2x.png" &>/dev/null
    done
    iconutil -c icns "$ICONSET" -o "$ICNS_OUT" 2>/dev/null
    rm -rf "$ICONSET"
fi

# Update setup.py to use .icns if it exists (only if not already set)
if [ -f "$ICNS_OUT" ] && ! grep -q "iconfile" "$PROJECT_DIR/setup.py"; then
    sed -i '' 's|"argv_emulation": False,|"argv_emulation": False, "iconfile": "assets/AppIcon.icns",|' "$PROJECT_DIR/setup.py"
fi

# Build the .app
"$VENV_DIR/bin/python3" setup.py py2app 2>&1 | tail -5

# py2app names the app after the script (main.app) — rename it
if [ -d "dist/main.app" ]; then
    mv "dist/main.app" "dist/Aloe Scribe.app"
fi

# Fix the CFBundleExecutable in the renamed app
if [ -d "dist/Aloe Scribe.app" ]; then
    mv "dist/Aloe Scribe.app/Contents/MacOS/main" "dist/Aloe Scribe.app/Contents/MacOS/Aloe Scribe" 2>/dev/null || true
    # Update the plist to match
    /usr/libexec/PlistBuddy -c "Set :CFBundleExecutable 'Aloe Scribe'" "dist/Aloe Scribe.app/Contents/Info.plist" 2>/dev/null || true
fi

# Install to /Applications
rm -rf "/Applications/Aloe Scribe.app"
cp -R "dist/Aloe Scribe.app" "/Applications/Aloe Scribe.app"

# Clear quarantine
xattr -cr "/Applications/Aloe Scribe.app" 2>/dev/null || true

echo "  Installed to /Applications/Aloe Scribe.app"

# -----------------------------------------------------------
# Done
# -----------------------------------------------------------
echo ""
echo "${BOLD}=========================================${NC}"
echo "${BOLD}  Setup complete!${NC}"
echo "${BOLD}=========================================${NC}"
echo ""
echo "  Open Aloe Scribe from:"
echo "    - Spotlight (Cmd+Space → 'Aloe Scribe')"
echo "    - /Applications/Aloe Scribe.app"
echo "    - Drag to Dock to pin it"
echo ""
echo "  To change audio devices later:"
echo "    cd ~/aloe-scribe && .venv/bin/python3 src/main.py --setup"
echo ""
echo "  Next steps:"
echo "    1. Open Aloe Scribe and click 'Start Recording Now' to capture a call."
echo "    2. (optional) Run: bash scripts/health-check-mac.sh"
echo ""
