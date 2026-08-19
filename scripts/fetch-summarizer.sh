#!/bin/bash
# fetch-summarizer.sh — Ensure the summarizer model is present locally (from
# GitHub Releases, not Hugging Face). Idempotent, shared by install-mac.sh
# and update-mac.sh. Non-fatal by design: without the model the app simply
# skips summaries.
#
# Usage: bash scripts/fetch-summarizer.sh

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MODEL_NAME="qwen3.5-4b-mlx-4bit"
MODEL_DIR="$PROJECT_DIR/models/$MODEL_NAME"
MODEL_TAG="model-$MODEL_NAME"
MODEL_BASE="https://github.com/vinnybarbs/Aloe-Scribe/releases/download/$MODEL_TAG"

mkdir -p "$MODEL_DIR"

if [ -f "$MODEL_DIR/model.safetensors" ] && [ -f "$MODEL_DIR/config.json" ]; then
    echo "  Summarizer model already present: $MODEL_DIR"
    exit 0
fi

echo "  Downloading the summarizer model from GitHub (~2.9 GB, one time)..."
(
    cd "$MODEL_DIR"
    curl -fL -# -O "$MODEL_BASE/SHA256SUMS"
    for f in $(awk '{print $2}' SHA256SUMS | grep -v '^model.safetensors.part-'); do
        [ "$f" = "model.safetensors" ] && continue
        curl -fL -# -O "$MODEL_BASE/$f"
    done
    for part in $(awk '{print $2}' SHA256SUMS | grep '^model.safetensors.part-'); do
        curl -fL -# -O "$MODEL_BASE/$part"
    done
    cat model.safetensors.part-* > model.safetensors
    rm -f model.safetensors.part-*
    EXPECTED="$(awk '$2=="model.safetensors"{print $1}' SHA256SUMS)"
    ACTUAL="$(shasum -a 256 model.safetensors | awk '{print $1}')"
    if [ "$EXPECTED" != "$ACTUAL" ]; then
        echo "  ✗ Summarizer model checksum mismatch. Re-run to retry." >&2
        rm -f model.safetensors
        exit 1
    fi
    echo "  ✓ Summarizer model verified: $MODEL_DIR"
)
