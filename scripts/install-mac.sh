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
# 3. BlackHole (system audio capture)
# -----------------------------------------------------------
echo -e "${GREEN}[3/7]${NC} Checking BlackHole..."
if brew list --cask blackhole-2ch &>/dev/null 2>&1; then
    echo "  BlackHole already installed"
else
    echo "  Installing BlackHole 2ch for system audio capture..."
    brew install --cask blackhole-2ch || true
    echo ""
    echo -e "${YELLOW}  IMPORTANT: You need to create a Multi-Output Device:${NC}"
    echo "  1. Open Audio MIDI Setup (Spotlight → 'Audio MIDI Setup')"
    echo "  2. Click '+' at bottom left → Create Multi-Output Device"
    echo "  3. Check BOTH your speakers/headphones AND 'BlackHole 2ch'"
    echo "  4. Right-click the Multi-Output Device → 'Use This Device For Sound Output'"
    echo ""
    read -p "  Press Enter when done (or skip for now)..."
fi

# -----------------------------------------------------------
# 4. Python venv + dependencies
# -----------------------------------------------------------
echo -e "${GREEN}[4/7]${NC} Setting up Python environment..."
python3 -m venv "$VENV_DIR" 2>/dev/null || python3.12 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -q \
    PyQt6>=6.6.0 \
    icalendar>=5.0.0 \
    requests>=2.31.0 \
    pillow>=10.0.0
echo "  Python venv + PyQt6 OK"

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

# Download model
MODEL="small"
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
CONFIG="$PROJECT_DIR/config/config.toml"

# Update whisper paths if they point to the default Linux location
if grep -q "/home/" "$CONFIG" 2>/dev/null; then
    sed -i '' "s|binary_path = .*|binary_path = \"$WHISPER_DIR/build/bin/whisper-cli\"|" "$CONFIG"
    sed -i '' "s|model_path = .*|model_path = \"$MODEL_PATH\"|" "$CONFIG"
    echo "  Config updated with macOS paths"
else
    echo "  Config already has correct paths"
fi

# -----------------------------------------------------------
# 7. Launchd service (autostart on login)
# -----------------------------------------------------------
echo -e "${GREEN}[7/7]${NC} Setting up autostart..."
PLIST_NAME="com.aloescribe.app"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$PLIST_NAME.plist"
mkdir -p "$PLIST_DIR"

cat > "$PLIST_PATH" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_DIR/bin/python3</string>
        <string>$PROJECT_DIR/src/main.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/aloe-scribe.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/aloe-scribe.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
EOF

# Load the service
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
echo "  Autostart enabled"

# -----------------------------------------------------------
# Done
# -----------------------------------------------------------
echo ""
echo "${BOLD}=========================================${NC}"
echo "${BOLD}  Setup complete!${NC}"
echo "${BOLD}=========================================${NC}"
echo ""
echo "  Aloe Scribe will start automatically on login."
echo ""
echo "  Manual controls:"
echo "    Start:   launchctl start com.aloescribe.app"
echo "    Stop:    launchctl stop com.aloescribe.app"
echo "    Logs:    tail -f /tmp/aloe-scribe.log"
echo "    Disable: launchctl unload ~/Library/LaunchAgents/com.aloescribe.app.plist"
echo ""
echo "  Next steps:"
echo "    1. Edit config/config.toml with your iCal URL"
echo "    2. Run: bash scripts/health-check-mac.sh"
echo "    3. Set up rclone for SharePoint: rclone config"
echo ""
