#!/bin/bash
# get-mac.sh — One-command installer for Aloe Scribe on macOS.
#
# Run (this form keeps the terminal interactive for Homebrew's prompts):
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/vinnybarbs/Aloe-Scribe/main/scripts/get-mac.sh)"
#
# What it does:
#   1. Checks the Mac is compatible (macOS 13+, warns on Intel).
#   2. Makes sure Xcode Command Line Tools exist (git + the Swift compiler).
#   3. Clones the repo to ~/aloe-scribe, or updates an existing checkout.
#      The location is fixed: the packaged app resolves its Python runtime
#      from ~/aloe-scribe/.venv (see src/main.py).
#   4. Creates the local code-signing identity so macOS keeps your Screen
#      Recording + Microphone permissions across app updates.
#   5. Hands off to scripts/install-mac.sh for the full install: Homebrew,
#      ffmpeg, Python deps (hash-verified), the transcription model
#      (downloaded from this repo's GitHub Releases, no Hugging Face), and
#      the /Applications app build.
#
# Everything is built locally on your machine, so there is no Gatekeeper
# quarantine to fight — no "app is damaged" dialogs, no right-click-Open.

set -e

REPO_URL="https://github.com/vinnybarbs/Aloe-Scribe.git"
# Fixed location — src/main.py looks for the runtime venv here. Do not change.
INSTALL_DIR="$HOME/aloe-scribe"

BOLD=$'\033[1m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'; NC=$'\033[0m'

echo ""
echo "${BOLD}Aloe Scribe — macOS installer${NC}"
echo ""

# -----------------------------------------------------------
# 1. Platform checks
# -----------------------------------------------------------
if [ "$(uname -s)" != "Darwin" ]; then
    echo "${RED}This installer is for macOS.${NC} On Windows use install-windows.ps1 (see README)."
    exit 1
fi

OS_MAJOR="$(sw_vers -productVersion | cut -d. -f1)"
if [ "${OS_MAJOR:-0}" -lt 13 ]; then
    echo "${RED}macOS 13 (Ventura) or newer is required${NC} — system audio capture uses ScreenCaptureKit."
    exit 1
fi

if [ "$(uname -m)" != "arm64" ]; then
    echo "${YELLOW}Intel Mac detected.${NC} The default Parakeet model needs Apple Silicon."
    echo "The installer will fall back to whisper.cpp (slower, but works)."
    echo ""
fi

# -----------------------------------------------------------
# 2. Xcode Command Line Tools (provides git and swiftc)
# -----------------------------------------------------------
if ! xcode-select -p >/dev/null 2>&1; then
    echo "${YELLOW}Xcode Command Line Tools are required${NC} (they provide git and the Swift compiler)."
    echo "macOS should now show an install dialog — click Install, wait for it to"
    echo "finish, then run this installer again."
    xcode-select --install >/dev/null 2>&1 || true
    exit 1
fi

# -----------------------------------------------------------
# 3. Clone or update the repo at ~/aloe-scribe
# -----------------------------------------------------------
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "${GREEN}Updating existing checkout${NC} at $INSTALL_DIR ..."
    git -C "$INSTALL_DIR" pull --ff-only || {
        echo "${YELLOW}Could not fast-forward (local changes?). Continuing with the current checkout.${NC}"
    }
elif [ -e "$INSTALL_DIR" ]; then
    echo "${RED}$INSTALL_DIR exists but is not a git checkout.${NC} Move it aside and re-run."
    exit 1
else
    echo "${GREEN}Cloning${NC} to $INSTALL_DIR ..."
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# -----------------------------------------------------------
# 4. Stable signing identity (keeps TCC permissions across updates)
# -----------------------------------------------------------
if bash scripts/create-signing-cert.sh; then
    :
else
    echo "${YELLOW}Signing-cert setup failed — continuing with ad-hoc signing.${NC}"
    echo "The app will still work, but macOS will re-ask for Screen Recording and"
    echo "Microphone permissions after each update."
fi

# -----------------------------------------------------------
# 5. Full install
# -----------------------------------------------------------
bash scripts/install-mac.sh
