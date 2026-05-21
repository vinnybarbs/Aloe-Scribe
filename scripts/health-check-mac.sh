#!/bin/bash
# Aloe Scribe — macOS Health Check
# Tests mic, ScreenCaptureKit helper, recording, and transcription end-to-end.

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'
PASS="${GREEN}PASS${NC}"
FAIL="${RED}FAIL${NC}"
WARN="${YELLOW}WARN${NC}"

TEST_WAV="/tmp/aloe-test-$(date +%s).wav"
WHISPER_BIN="$HOME/whisper.cpp/build/bin/whisper-cli"
WHISPER_MODEL="$HOME/whisper.cpp/models/ggml-small.bin"
RECORD_SECONDS=8

echo ""
echo "========================================="
echo "  Aloe Scribe macOS Health Check"
echo "========================================="
echo ""

# -----------------------------------------------------------
# 1. ffmpeg
# -----------------------------------------------------------
printf "%-40s" "1. ffmpeg installed"
if command -v ffmpeg &>/dev/null; then
    echo -e "[$PASS]"
else
    echo -e "[$FAIL] Install with: brew install ffmpeg"
    exit 1
fi

# -----------------------------------------------------------
# 2. Audio devices
# -----------------------------------------------------------
printf "%-40s" "2. Audio input devices"
DEVICES=$(ffmpeg -f avfoundation -list_devices true -i "" 2>&1 || true)
if echo "$DEVICES" | grep -q "AVFoundation audio devices"; then
    AUDIO_COUNT=$(echo "$DEVICES" | grep -A50 "AVFoundation audio devices" | grep -c "\[" || true)
    echo -e "[$PASS] $AUDIO_COUNT device(s) found"
else
    echo -e "[$FAIL] No audio devices detected"
    exit 1
fi

# -----------------------------------------------------------
# 3. ScreenCaptureKit audio helper (replaces the old BlackHole check)
# -----------------------------------------------------------
printf "%-40s" "3. SCK audio helper"
SCK_HELPER="$HOME/aloe-scribe/bin/aloe-audio-capture"
if [ -x "$SCK_HELPER" ]; then
    echo -e "[$PASS] $SCK_HELPER"
elif [ -x "/Applications/Aloe Scribe.app/Contents/Resources/bin/aloe-audio-capture" ]; then
    echo -e "[$PASS] (bundled in .app)"
else
    echo -e "[$WARN] Helper not built yet — run: bash scripts/build-helper.sh"
fi

# -----------------------------------------------------------
# 4. Whisper binary
# -----------------------------------------------------------
printf "%-40s" "4. whisper.cpp binary"
if [ -x "$WHISPER_BIN" ]; then
    echo -e "[$PASS]"
else
    echo -e "[$FAIL] Not found at $WHISPER_BIN"
    exit 1
fi

# -----------------------------------------------------------
# 5. Metal acceleration
# -----------------------------------------------------------
printf "%-40s" "5. Metal GPU acceleration"
# Check if whisper was built with Metal support
if otool -L "$WHISPER_BIN" 2>/dev/null | grep -q "Metal\|Accelerate"; then
    echo -e "[$PASS] Metal linked"
elif [ -f "$HOME/whisper.cpp/build/bin/ggml-metal.metal" ] || [ -f "$HOME/whisper.cpp/build/ggml/src/ggml-metal-embed.metal" ]; then
    echo -e "[$PASS] Metal shaders found"
else
    echo -e "[$WARN] Metal not detected — CPU only"
    echo "         Rebuild with: cmake -B build -DWHISPER_METAL=ON"
fi

# -----------------------------------------------------------
# 6. Whisper model
# -----------------------------------------------------------
printf "%-40s" "6. Whisper model"
if [ -f "$WHISPER_MODEL" ]; then
    SIZE=$(du -h "$WHISPER_MODEL" | awk '{print $1}')
    echo -e "[$PASS] $SIZE — $(basename "$WHISPER_MODEL")"
else
    echo -e "[$FAIL] Not found at $WHISPER_MODEL"
    exit 1
fi

# -----------------------------------------------------------
# 7. PyQt6
# -----------------------------------------------------------
printf "%-40s" "7. PyQt6"
VENV_PYTHON="$(dirname "$0")/../.venv/bin/python3"
if [ -x "$VENV_PYTHON" ]; then
    if "$VENV_PYTHON" -c "from PyQt6.QtWidgets import QApplication; print('ok')" 2>/dev/null | grep -q ok; then
        echo -e "[$PASS]"
    else
        echo -e "[$FAIL] PyQt6 not installed in venv"
        echo "         Run: .venv/bin/pip install PyQt6"
    fi
else
    # Try system python
    if python3 -c "from PyQt6.QtWidgets import QApplication" 2>/dev/null; then
        echo -e "[$PASS] (system python)"
    else
        echo -e "[$FAIL] PyQt6 not found"
    fi
fi

# -----------------------------------------------------------
# 8. Record test audio
# -----------------------------------------------------------
echo ""
echo "-----------------------------------------"
echo "  Recording ${RECORD_SECONDS}s of audio..."
echo "  Speak clearly into your mic NOW!"
echo "-----------------------------------------"
echo ""

# Find the actual microphone (not BlackHole)
MIC_IDX=$(ffmpeg -f avfoundation -list_devices true -i "" 2>&1 | \
    grep -A50 "AVFoundation audio devices" | \
    grep -iv "blackhole" | \
    grep -o '\[[0-9]*\]' | head -1 | tr -d '[]')
MIC_IDX=${MIC_IDX:-0}
echo "  Using audio device index: :${MIC_IDX}"

ffmpeg -y \
    -f avfoundation -i ":${MIC_IDX}" \
    -ar 16000 -ac 1 -c:a pcm_s16le \
    -t "$RECORD_SECONDS" \
    "$TEST_WAV" 2>/dev/null

printf "%-40s" "8. Audio recorded"
if [ -f "$TEST_WAV" ]; then
    SIZE=$(du -h "$TEST_WAV" | awk '{print $1}')
    DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$TEST_WAV" 2>/dev/null | cut -d. -f1)
    echo -e "[$PASS] ${DURATION}s, ${SIZE}"
else
    echo -e "[$FAIL] Recording failed"
    exit 1
fi

# -----------------------------------------------------------
# 9. Check audio isn't silent
# -----------------------------------------------------------
printf "%-40s" "9. Audio has signal (not silent)"
VOLUME=$(ffmpeg -i "$TEST_WAV" -af "volumedetect" -f null /dev/null 2>&1 | grep mean_volume | awk '{print $5}')
if [ -n "$VOLUME" ]; then
    VOLUME_INT=${VOLUME%.*}
    if [ "$VOLUME_INT" -gt -60 ]; then
        echo -e "[$PASS] ${VOLUME} dB"
    else
        echo -e "[$WARN] ${VOLUME} dB — very quiet, check mic level"
        echo "         System Preferences → Sound → Input"
    fi
else
    echo -e "[$WARN] Could not measure volume"
fi

# -----------------------------------------------------------
# 10. Transcribe
# -----------------------------------------------------------
echo ""
echo "-----------------------------------------"
echo "  Transcribing with Whisper (small)..."
echo "-----------------------------------------"
echo ""

TRANSCRIPT=$("$WHISPER_BIN" -m "$WHISPER_MODEL" -f "$TEST_WAV" -l en --no-timestamps 2>/dev/null || true)

printf "%-40s" "10. Transcription"
if [ -n "$TRANSCRIPT" ]; then
    echo -e "[$PASS]"
else
    echo -e "[$FAIL] Whisper produced no output"
    rm -f "$TEST_WAV"
    exit 1
fi

# -----------------------------------------------------------
# Results
# -----------------------------------------------------------
echo ""
echo "========================================="
echo "  Transcript:"
echo "-----------------------------------------"
echo "$TRANSCRIPT"
echo ""
echo "========================================="
echo ""
echo -e "${GREEN}All checks passed.${NC} If the transcript"
echo "above matches what you said, you're good!"
echo ""

# Cleanup
rm -f "$TEST_WAV"
