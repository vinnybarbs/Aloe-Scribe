#!/bin/bash
# publish-model.sh — Publish the Parakeet model weights as a GitHub Release so
# the installer can fetch them WITHOUT Hugging Face.
#
# GitHub caps a single release asset at 2 GiB, and model.safetensors is ~2.3 GiB,
# so this splits it into <2 GiB parts, writes a SHA256SUMS manifest, and uploads
# config.json + the parts + the manifest to a release tagged for the model.
# scripts/install-mac.sh downloads these, reassembles, and verifies the hash.
#
# Run once (re-run to refresh — it replaces the existing release).
#
# Model source (in priority order):
#   1. $1  — a local dir containing config.json + model.safetensors
#   2. the local Hugging Face cache (mlx-community/parakeet-tdt-0.6b-v3)
#
# Usage:
#   bash scripts/publish-model.sh                 # use the HF cache copy
#   bash scripts/publish-model.sh /path/to/model  # use a local dir

set -e

REPO="vinnybarbs/Aloe-Scribe"
MODEL_NAME="parakeet-tdt-0.6b-v3"
TAG="model-${MODEL_NAME}"
PART_SIZE="1900m"   # 1900 MiB — comfortably under GitHub's 2 GiB asset limit

# --- locate the source files ---
SRC="$1"
if [ -z "$SRC" ]; then
    SRC="$(find "$HOME/.cache/huggingface/hub/models--mlx-community--${MODEL_NAME}/snapshots" \
        -maxdepth 1 -mindepth 1 -type d 2>/dev/null | head -1)"
fi
CONFIG="$SRC/config.json"
WEIGHTS="$SRC/model.safetensors"
if [ ! -f "$CONFIG" ] || [ ! -f "$WEIGHTS" ]; then
    echo "Error: config.json + model.safetensors not found in: ${SRC:-<none>}" >&2
    echo "Pass the model directory as the first argument." >&2
    exit 1
fi

echo "Source model dir: $SRC"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cp -L "$CONFIG" "$WORK/config.json"

echo "Splitting model.safetensors into ${PART_SIZE} parts..."
# split reads through the HF symlink automatically.
split -b "$PART_SIZE" "$WEIGHTS" "$WORK/model.safetensors.part-"

echo "Computing checksums..."
(
    cd "$WORK"
    shasum -a 256 config.json model.safetensors.part-* > SHA256SUMS
    # Full-file checksum for post-reassembly verification on the install side.
    FULL="$(shasum -a 256 "$WEIGHTS" | awk '{print $1}')"
    echo "${FULL}  model.safetensors" >> SHA256SUMS
)

echo "Release assets to upload:"
ls -lh "$WORK"

# --- create (or replace) the release ---
if gh release view "$TAG" -R "$REPO" >/dev/null 2>&1; then
    echo "Replacing existing release $TAG..."
    gh release delete "$TAG" -R "$REPO" --yes --cleanup-tag
fi

echo "Creating release $TAG on $REPO..."
gh release create "$TAG" -R "$REPO" \
    --title "Parakeet model — ${MODEL_NAME}" \
    --notes "Parakeet TDT 0.6b v3 (MLX) weights for offline install — no Hugging Face required. Fetched automatically by scripts/install-mac.sh. License: CC-BY-4.0 (NVIDIA / mlx-community)." \
    "$WORK/config.json" "$WORK"/model.safetensors.part-* "$WORK/SHA256SUMS"

echo ""
echo "Published. Assets:"
gh release view "$TAG" -R "$REPO" --json assets -q '.assets[] | "  \(.name)  (\(.size) bytes)"'
echo ""
echo "Install side will fetch from:"
echo "  https://github.com/${REPO}/releases/download/${TAG}/"
