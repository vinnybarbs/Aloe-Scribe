#!/bin/bash
# publish-summarizer.sh — Publish the summarizer model weights as a GitHub
# Release so installs fetch them without Hugging Face. Splits the big
# safetensors file under GitHub's 2 GiB asset cap, mirrors publish-model.sh.
#
# Usage: bash scripts/publish-summarizer.sh [/path/to/model/dir]

set -e

REPO="vinnybarbs/Aloe-Scribe"
MODEL_NAME="qwen3.5-4b-mlx-4bit"
TAG="model-${MODEL_NAME}"
PART_SIZE="1900m"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${1:-$PROJECT_DIR/models/$MODEL_NAME}"

[ -f "$SRC/model.safetensors" ] || { echo "No model.safetensors in $SRC" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
echo "Staging in $WORK ..."
for f in "$SRC"/*; do
    base="$(basename "$f")"
    [ "$base" = "model.safetensors" ] && continue
    cp "$f" "$WORK/"
done
split -b "$PART_SIZE" "$SRC/model.safetensors" "$WORK/model.safetensors.part-"
(
    cd "$WORK"
    shasum -a 256 * > SHA256SUMS.tmp
    (cd "$SRC" && shasum -a 256 model.safetensors) >> SHA256SUMS.tmp
    mv SHA256SUMS.tmp SHA256SUMS
)

gh release delete "$TAG" --repo "$REPO" --yes 2>/dev/null || true
gh release create "$TAG" --repo "$REPO" \
    --title "Summarizer model · $MODEL_NAME" \
    --notes "Qwen 3.5 4B (4-bit MLX) weights for the local executive summarizer. Fetched by scripts/fetch-summarizer.sh, no Hugging Face required. Source: mlx-community/Qwen3.5-4B-MLX-4bit. License: Apache 2.0 (Qwen)." \
    "$WORK"/*
echo "Published release $TAG"
