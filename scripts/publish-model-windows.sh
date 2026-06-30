#!/bin/bash
# publish-model-windows.sh — Publish the faster-whisper (CTranslate2) model as a
# GitHub Release so the Windows installer can fetch it WITHOUT Hugging Face.
#
# The CT2 model folder (config.json, model.bin, tokenizer.json, vocabulary.json,
# preprocessor_config.json) is uploaded as release assets with a SHA256SUMS
# manifest. model.bin is ~1.4 GiB, under GitHub's 2 GiB asset limit, so no
# splitting is needed. scripts\fetch-model-windows.ps1 downloads and verifies.
#
# Run once (re-run to refresh — it replaces the existing release).
#
# Usage:
#   bash scripts/publish-model-windows.sh /path/to/ct2-model-dir
#   bash scripts/publish-model-windows.sh        # uses the HF cache copy

set -e

REPO="vinnybarbs/Aloe-Scribe"
MODEL_NAME="faster-distil-whisper-large-v3"
HF_ID="Systran/${MODEL_NAME}"
TAG="model-${MODEL_NAME}"

# Files faster-whisper needs to load a model from a local folder.
FILES=(config.json model.bin tokenizer.json vocabulary.json preprocessor_config.json)

SRC="$1"
if [ -z "$SRC" ]; then
    SRC="$(find "$HOME/.cache/huggingface/hub/models--Systran--${MODEL_NAME}/snapshots" \
        -maxdepth 1 -mindepth 1 -type d 2>/dev/null | head -1)"
fi
if [ ! -f "$SRC/model.bin" ] || [ ! -f "$SRC/config.json" ]; then
    echo "Error: model.bin + config.json not found in: ${SRC:-<none>}" >&2
    echo "Pass the CT2 model directory as the first argument." >&2
    exit 1
fi
echo "Source model dir: $SRC"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
for f in "${FILES[@]}"; do
    if [ -f "$SRC/$f" ]; then
        cp -L "$SRC/$f" "$WORK/$f"
    else
        echo "  (skipping missing optional file: $f)"
    fi
done

echo "Computing checksums..."
( cd "$WORK" && shasum -a 256 * > SHA256SUMS )

echo "Release assets to upload:"
ls -lh "$WORK"

if gh release view "$TAG" -R "$REPO" >/dev/null 2>&1; then
    echo "Replacing existing release $TAG..."
    gh release delete "$TAG" -R "$REPO" --yes --cleanup-tag
fi

echo "Creating release $TAG on $REPO..."
gh release create "$TAG" -R "$REPO" \
    --title "faster-whisper model — ${MODEL_NAME}" \
    --notes "CTranslate2 distil-whisper-large-v3 weights for the Windows build's offline install — no Hugging Face required. Fetched automatically by scripts/fetch-model-windows.ps1. Source: ${HF_ID}. License: MIT (Systran / distil-whisper)." \
    "$WORK"/*

echo ""
echo "Published. Assets:"
gh release view "$TAG" -R "$REPO" --json assets -q '.assets[] | "  \(.name)  (\(.size) bytes)"'
