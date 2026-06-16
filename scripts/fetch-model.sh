#!/bin/bash
# fetch-model.sh — Ensure the Parakeet model is present locally (from GitHub,
# NOT Hugging Face) and point config/config.toml at it.
#
# Idempotent and shared by install-mac.sh (fresh install) and update-mac.sh
# (existing users migrating off the Hugging Face copy). If the model is already
# in models/<name> it just makes sure the config path is set and exits.
#
# Usage: bash scripts/fetch-model.sh

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MODEL_NAME="parakeet-tdt-0.6b-v3"
MODEL_DIR="$PROJECT_DIR/models/$MODEL_NAME"
MODEL_TAG="model-$MODEL_NAME"
MODEL_BASE="https://github.com/vinnybarbs/Aloe-Scribe/releases/download/$MODEL_TAG"
CONFIG="$PROJECT_DIR/config/config.toml"

mkdir -p "$MODEL_DIR"

if [ -f "$MODEL_DIR/model.safetensors" ] && [ -f "$MODEL_DIR/config.json" ]; then
    echo "  Parakeet model already present: $MODEL_DIR"
else
    echo "  Downloading Parakeet model from GitHub (no Hugging Face)..."
    (
        cd "$MODEL_DIR"
        curl -fL -# -O "$MODEL_BASE/SHA256SUMS"
        curl -fL -# -O "$MODEL_BASE/config.json"
        echo "  Downloading model parts (~2.3 GB)..."
        for part in $(awk '{print $2}' SHA256SUMS | grep '^model.safetensors.part-'); do
            curl -fL -# -O "$MODEL_BASE/$part"
        done
        echo "  Reassembling model.safetensors..."
        cat model.safetensors.part-* > model.safetensors
        rm -f model.safetensors.part-*
        EXPECTED="$(awk '$2=="model.safetensors"{print $1}' SHA256SUMS)"
        ACTUAL="$(shasum -a 256 model.safetensors | awk '{print $1}')"
        if [ "$EXPECTED" != "$ACTUAL" ]; then
            echo "  ✗ Model checksum mismatch — download corrupt. Re-run to retry." >&2
            rm -f model.safetensors
            exit 1
        fi
        echo "  ✓ Model verified: $MODEL_DIR"
    )
fi

# Point the app at the local model path (replaces any Hugging Face id).
if [ -f "$CONFIG" ]; then
    sed -i '' "s|^parakeet_model = .*|parakeet_model = \"$MODEL_DIR\"|" "$CONFIG"
fi
