#!/usr/bin/env bash
# install.sh — Set up Aloe Scribe on Linux
# Run once: bash scripts/install.sh

set -e
BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RESET="\033[0m"

echo -e "\n${BOLD}🌿 Aloe Scribe — Linux Installer${RESET}\n"

# config/config.toml is per-user and untracked (see .gitignore). Seed it from
# the committed template on a fresh install so later steps can edit it.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ ! -f "$PROJECT_DIR/config/config.toml" ]; then
    cp "$PROJECT_DIR/config/config.toml.example" "$PROJECT_DIR/config/config.toml"
fi

# ---------------------------------------------------------------------------
# 1. System dependencies
# ---------------------------------------------------------------------------
echo -e "${BOLD}[1/5] Installing system packages...${RESET}"
sudo apt-get update -q
sudo apt-get install -y \
    ffmpeg \
    rclone \
    libnotify-bin \
    python3-pip \
    python3-gi \
    gir1.2-appindicator3-0.1 \
    libayatana-appindicator3-1 \
    git \
    cmake \
    build-essential

echo -e "${GREEN}✓ System packages installed${RESET}"

# ---------------------------------------------------------------------------
# 2. Python dependencies (virtualenv to avoid system pip restrictions)
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}[2/5] Installing Python packages...${RESET}"
sudo apt-get install -y python3-full python3-venv -q

VENV_DIR="$SCRIPT_DIR/../.venv"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

pip install --quiet pillow tomli

echo -e "${GREEN}✓ Python packages installed in .venv${RESET}"

# ---------------------------------------------------------------------------
# 3. whisper.cpp
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}[3/5] Building whisper.cpp...${RESET}"
WHISPER_DIR="$HOME/.local/share/aloe-scribe/whisper.cpp"

if [ ! -f "$WHISPER_DIR/main" ]; then
    mkdir -p "$HOME/.local/share/aloe-scribe"
    git clone https://github.com/ggerganov/whisper.cpp "$WHISPER_DIR"
    cd "$WHISPER_DIR"
    make -j$(nproc)
    echo -e "${GREEN}✓ whisper.cpp built${RESET}"
else
    echo -e "${GREEN}✓ whisper.cpp already built${RESET}"
fi

# Download base model
MODEL_DIR="$HOME/.local/share/aloe-scribe/models"
mkdir -p "$MODEL_DIR"

if [ ! -f "$MODEL_DIR/ggml-base.bin" ]; then
    echo -e "\n${BOLD}Downloading Whisper base model (~142 MB)...${RESET}"
    cd "$WHISPER_DIR"
    bash models/download-ggml-model.sh base
    cp models/ggml-base.bin "$MODEL_DIR/"
    echo -e "${GREEN}✓ Model downloaded${RESET}"
else
    echo -e "${GREEN}✓ Model already present${RESET}"
fi

# Update config with resolved paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/../config/config.toml"

sed -i "s|binary_path = .*|binary_path = \"$WHISPER_DIR/main\"|" "$CONFIG"
sed -i "s|model_path = .*|model_path = \"$MODEL_DIR/ggml-base.bin\"|" "$CONFIG"

# ---------------------------------------------------------------------------
# 4. rclone SharePoint setup
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}[4/5] rclone SharePoint setup${RESET}"
echo -e "${YELLOW}You'll need to configure rclone for SharePoint manually.${RESET}"
echo ""
echo "Run this now? (y/n)"
read -r answer
if [[ "$answer" == "y" ]]; then
    echo -e "\nWhen prompted:"
    echo "  Storage type    → Microsoft OneDrive"
    echo "  Drive type      → SharePoint document library"
    echo "  Remote name     → sharepoint  (or whatever you prefer)"
    echo ""
    rclone config
    echo ""
    echo "Once configured, set [sync] enabled = true and update rclone_remote in config.toml"
fi

# ---------------------------------------------------------------------------
# 5. systemd autostart
# ---------------------------------------------------------------------------
echo -e "\n${BOLD}[5/5] Setting up autostart...${RESET}"
SYSTEMD_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_DIR"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAIN_PY="$SCRIPT_DIR/../src/main.py"

cat > "$SYSTEMD_DIR/aloe-scribe.service" << EOF
[Unit]
Description=Aloe Scribe — Meeting Transcription
After=graphical-session.target

[Service]
Type=simple
ExecStart=$SCRIPT_DIR/../.venv/bin/python3 $MAIN_PY
Restart=on-failure
RestartSec=5
Environment=DISPLAY=:0
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%i/bus

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable aloe-scribe.service

echo -e "${GREEN}✓ Systemd service installed${RESET}"
echo ""
echo -e "${BOLD}Done! Next steps:${RESET}"
echo ""
echo "  1. Edit config:  nano $SCRIPT_DIR/../config/config.toml"
echo "     → Paste your iCal URL under [calendar]"
echo "     → Set [sync] enabled = true once rclone is configured"
echo ""
echo "  2. Start now:    systemctl --user start aloe-scribe"
echo "     Check logs:   journalctl --user -u aloe-scribe -f"
echo ""
echo "  3. Or run directly for testing:"
echo "     python3 $MAIN_PY"
echo ""
echo -e "${GREEN}🌿 Aloe Scribe is ready${RESET}"
