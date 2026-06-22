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

# config/config.toml is per-user and untracked (see .gitignore). Seed it from
# the committed template on a fresh install so the steps below can edit it.
if [ ! -f "$PROJECT_DIR/config/config.toml" ]; then
    cp "$PROJECT_DIR/config/config.toml.example" "$PROJECT_DIR/config/config.toml"
fi

echo ""
echo "${BOLD}=========================================${NC}"
echo "${BOLD}  Aloe Scribe — macOS Setup${NC}"
echo "${BOLD}=========================================${NC}"
echo ""

# -----------------------------------------------------------
# 1. Homebrew
# -----------------------------------------------------------
echo -e "${GREEN}[1/6]${NC} Checking Homebrew..."
if ! command -v brew &>/dev/null; then
    echo "Homebrew not found. Installing..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi
echo "  Homebrew OK"

# -----------------------------------------------------------
# 2. System dependencies
# -----------------------------------------------------------
echo -e "${GREEN}[2/6]${NC} Installing system dependencies..."
brew install ffmpeg rclone python@3.12 cmake git 2>/dev/null || true
echo "  ffmpeg, rclone, python, cmake OK"

# -----------------------------------------------------------
# 3. System audio capture
# -----------------------------------------------------------
echo -e "${GREEN}[3/6]${NC} System audio capture..."
echo "  Uses ScreenCaptureKit (macOS 13+) — no BlackHole or Multi-Output"
echo "  Device required. On first launch you'll be prompted for Screen"
echo "  Recording permission (used only for audio; no video is captured)."

# -----------------------------------------------------------
# 4. Python venv + dependencies
# -----------------------------------------------------------
echo -e "${GREEN}[4/6]${NC} Setting up Python environment..."
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

# Install Python dependencies. On Apple Silicon we install from a fully
# hash-pinned lockfile (requirements-mac.txt) with --require-hashes, so every
# package AND transitive dependency is verified against a known SHA256 —
# reproducible and tamper-evident (a swapped/poisoned wheel is rejected).
# Regenerate the lockfile when bumping versions:
#   uv pip compile requirements-mac.in --generate-hashes -o requirements-mac.txt
#
# parakeet-mlx (in the lockfile) pulls in mlx, which is Apple Silicon only —
# on Intel we install a loose base set and fall back to whisper.cpp.
ARCH="$(uname -m)"
if [ "$ARCH" = "arm64" ]; then
    echo "  Installing pinned, hash-verified dependencies..."
    if "$VENV_DIR/bin/pip" install --require-hashes -r "$PROJECT_DIR/requirements-mac.txt"; then
        # pip "success" doesn't always mean the native libs load (Metal, etc.).
        if "$VENV_DIR/bin/python3" -c "import parakeet_mlx" 2>/dev/null; then
            echo "  ✓ Dependencies + parakeet-mlx ready (all SHA256-verified)"
        else
            echo "  ✗ parakeet-mlx installed but failed to import — falling back to whisper"
            INSTALL_WHISPER=1
        fi
    else
        echo "  ✗ Hash-verified install failed — falling back to whisper"
        INSTALL_WHISPER=1
    fi
else
    echo "  ⚠ Detected Intel Mac (arch=$ARCH) — parakeet-mlx requires Apple Silicon."
    echo "    Installing base deps + falling back to whisper.cpp."
    "$VENV_DIR/bin/pip" install -q \
        "PyQt6>=6.6.0" "pyobjc-framework-Cocoa>=10.0" "pillow>=10.0.0" \
        "tomli>=2.0.0" "py2app>=0.28.0"
    INSTALL_WHISPER=1
    sed -i '' 's|^backend\s*=\s*"parakeet"|backend = "whisper"|' "$PROJECT_DIR/config/config.toml"
fi
echo "  Python venv OK ($(${VENV_DIR}/bin/python3 --version))"

# -----------------------------------------------------------
# 5. whisper.cpp (optional, off by default)
# -----------------------------------------------------------
# Parakeet TDT (installed above via parakeet-mlx) is the default backend.
# whisper.cpp is a fallback — install only if INSTALL_WHISPER=1 is set or
# the existing config explicitly selects the whisper backend.
echo -e "${GREEN}[5/6]${NC} whisper.cpp (optional fallback)..."
CONFIG="$PROJECT_DIR/config/config.toml"
WANT_WHISPER=0
if [ "${INSTALL_WHISPER:-0}" = "1" ]; then
    WANT_WHISPER=1
elif grep -qE '^backend\s*=\s*"whisper"' "$CONFIG" 2>/dev/null; then
    WANT_WHISPER=1
fi

if [ "$WANT_WHISPER" = "1" ]; then
    echo "  Building whisper.cpp with Metal acceleration..."
    if [ -d "$WHISPER_DIR" ]; then
        cd "$WHISPER_DIR" && git pull -q
    else
        git clone https://github.com/ggerganov/whisper.cpp.git "$WHISPER_DIR"
        cd "$WHISPER_DIR"
    fi
    cmake -B build -DWHISPER_METAL=ON -DCMAKE_BUILD_TYPE=Release 2>/dev/null
    cmake --build build --config Release -j$(sysctl -n hw.ncpu) 2>/dev/null
    MODEL="large-v3-turbo"
    MODEL_PATH="$WHISPER_DIR/models/ggml-${MODEL}.bin"
    if [ ! -f "$MODEL_PATH" ]; then
        bash "$WHISPER_DIR/models/download-ggml-model.sh" "$MODEL"
    fi
    cd "$PROJECT_DIR"
    sed -i '' "s|binary_path = .*|binary_path = \"$WHISPER_DIR/build/bin/whisper-cli\"|" "$CONFIG"
    sed -i '' "s|model_path = .*|model_path = \"$MODEL_PATH\"|" "$CONFIG"
    echo "  whisper.cpp + $MODEL installed"
else
    echo "  Skipped (default is Parakeet). To install later, rerun:"
    echo "    INSTALL_WHISPER=1 bash scripts/install-mac.sh"
fi

# Output directory — point local_dir to ~/meetings by default; the user
# can change this via the folder picker in the app's idle screen.
DEFAULT_OUTPUT="$HOME/meetings"
mkdir -p "$DEFAULT_OUTPUT"
sed -i '' "s|local_dir = .*|local_dir = \"$DEFAULT_OUTPUT\"|" "$CONFIG"
echo "  Transcripts will save to: $DEFAULT_OUTPUT (changeable in the app)"

# -----------------------------------------------------------
# Parakeet model weights — fetched from GitHub, NOT Hugging Face
# -----------------------------------------------------------
# Hosted as a GitHub Release (see scripts/publish-model.sh) so installs work
# where Hugging Face is blocked. Skipped when the whisper backend is selected.
if [ "$WANT_WHISPER" != "1" ]; then
    echo -e "${GREEN}[Model]${NC} Parakeet weights (from GitHub, no Hugging Face)..."
    bash "$PROJECT_DIR/scripts/fetch-model.sh"
fi

# -----------------------------------------------------------
# 6. Build native .app bundle
# -----------------------------------------------------------
echo -e "${GREEN}[6/6]${NC} Building Aloe Scribe.app via scripts/build-app.sh..."

cd "$PROJECT_DIR"

# Clean previous builds and caches.
rm -rf build dist *.egg-info .eggs
rm -rf /Applications/Aloe\ Scribe.app
find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# Generate .icns icon from PNG (the .app bundle's dock + tray icon)
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

# Delegate the actual build + code-signing + install to scripts/build-app.sh.
# That script compiles the Swift audio helper, runs py2app with the right
# excludes, signs everything with stable ad-hoc identifiers, and installs
# to /Applications. Keeping the logic there means future rebuilds don't
# have to re-run this whole install script.
bash scripts/build-app.sh

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
