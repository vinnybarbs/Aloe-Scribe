#!/bin/bash
# update-mac.sh — Update Aloe Scribe to the latest version (macOS).
#
# One command to:
#   1. Pull the newest code from GitHub.
#   2. Preserve your personal settings (calendar URL, mic, output folder).
#   3. Rebuild + reinstall /Applications/Aloe Scribe.app.
#
# Why step 2 matters: your live settings live INSIDE the installed app bundle
# (the app reads/writes config there), while the rebuild bakes the repo's
# config/config.toml into the new bundle. So before rebuilding we copy your
# live settings out of the current bundle into the repo — otherwise an update
# would reset them to defaults.
#
# Usage: bash scripts/update-mac.sh

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

APP_NAME="Aloe Scribe"
INSTALLED_CONFIG="/Applications/${APP_NAME}.app/Contents/Resources/config/config.toml"
REPO_CONFIG="$PROJECT_DIR/config/config.toml"
TEMPLATE="$PROJECT_DIR/config/config.toml.example"

echo ""
echo -e "${GREEN}[1/5]${NC} Fetching the latest version from GitHub..."
if ! git pull --ff-only origin main; then
    echo ""
    echo -e "${YELLOW}Couldn't fast-forward.${NC} You may have uncommitted code changes." >&2
    echo "Resolve them (or 'git stash' them) and re-run: bash scripts/update-mac.sh" >&2
    exit 1
fi

echo -e "${GREEN}[2/5]${NC} Preserving your current settings..."
if [ -f "$INSTALLED_CONFIG" ]; then
    # Carry the live settings from the installed bundle into the repo so the
    # rebuild bakes YOUR settings, not the template defaults.
    cp "$INSTALLED_CONFIG" "$REPO_CONFIG"
    echo "  Kept settings from the installed app."
elif [ ! -f "$REPO_CONFIG" ]; then
    # No installed app and no local config (fresh clone) — start from template.
    cp "$TEMPLATE" "$REPO_CONFIG"
    echo "  No existing settings found — started from the template."
else
    echo "  Using existing config/config.toml."
fi

echo -e "${GREEN}[3/5]${NC} Ensuring the Parakeet model is local (no Hugging Face)..."
if grep -qE '^backend[[:space:]]*=[[:space:]]*"parakeet"' "$REPO_CONFIG" 2>/dev/null; then
    # Migrates older installs that still point at the Hugging Face model id onto
    # the GitHub-hosted local copy. No-op once the model is already local.
    bash "$PROJECT_DIR/scripts/fetch-model.sh"
else
    echo "  Backend is not parakeet — skipping."
fi

echo -e "${GREEN}[4/5]${NC} Syncing Python dependencies..."
# New app versions can need new packages (e.g. sherpa-onnx for speaker
# labels). Idempotent and fast when nothing changed. Hash-verified on Apple
# Silicon, same as install-mac.sh. Non-fatal: optional deps degrade
# gracefully, so a network hiccup here shouldn't block the update.
VENV_DIR="$PROJECT_DIR/.venv"
if [ -x "$VENV_DIR/bin/pip" ]; then
    if [ "$(uname -m)" = "arm64" ]; then
        "$VENV_DIR/bin/pip" install -q --require-hashes -r "$PROJECT_DIR/requirements-mac.txt" \
            && echo "  Dependencies up to date." \
            || echo -e "  ${YELLOW}⚠ Dependency sync failed — the app still works; optional features may be off. Re-run the update to retry.${NC}"
    else
        echo "  Intel Mac — skipping (whisper fallback has no new deps)."
    fi
else
    echo -e "  ${YELLOW}⚠ No venv at $VENV_DIR — run scripts/install-mac.sh for a full install.${NC}"
fi

echo -e "${GREEN}[5/5]${NC} Rebuilding and reinstalling the app (~1 minute)..."
# Ensure the stable signing identity exists (once) so this and future updates
# don't reset Screen Recording / Microphone permissions. Non-fatal.
bash scripts/create-signing-cert.sh || true
bash scripts/build-app.sh

echo ""
echo -e "${GREEN}✅ Update complete.${NC} Open ${APP_NAME} from Spotlight (Cmd+Space)."
echo ""
