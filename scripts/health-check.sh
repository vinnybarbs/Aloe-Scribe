#!/bin/bash
# Aloe Scribe — Health Check
# Tests mic, system audio, recording, and transcription end-to-end.

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
echo "  Aloe Scribe Health Check"
echo "========================================="
echo ""

# -----------------------------------------------------------
# 1. ffmpeg
# -----------------------------------------------------------
printf "%-40s" "1. ffmpeg installed"
if command -v ffmpeg &>/dev/null; then
    echo -e "[$PASS]"
else
    echo -e "[$FAIL] Install with: sudo apt install ffmpeg"
    exit 1
fi

# -----------------------------------------------------------
# 2. Default mic
# -----------------------------------------------------------
printf "%-40s" "2. Default microphone"
MIC=$(pactl get-default-source 2>/dev/null || true)
if [ -n "$MIC" ]; then
    echo -e "[$PASS] $MIC"
else
    echo -e "[$FAIL] No default source found"
    exit 1
fi

# -----------------------------------------------------------
# 3. System audio monitor
# -----------------------------------------------------------
printf "%-40s" "3. System audio monitor"
MONITOR=$(pactl list short sources 2>/dev/null | grep '\.monitor' | head -1 | awk '{print $2}' || true)
if [ -n "$MONITOR" ]; then
    echo -e "[$PASS] $MONITOR"
else
    echo -e "[$WARN] Not found — mic-only recording"
fi

# -----------------------------------------------------------
# 4. Whisper binary
# -----------------------------------------------------------
printf "%-40s" "4. whisper.cpp binary"
if [ -x "$WHISPER_BIN" ]; then
    echo -e "[$PASS] $WHISPER_BIN"
else
    echo -e "[$FAIL] Not found at $WHISPER_BIN"
    exit 1
fi

# -----------------------------------------------------------
# 5. Whisper model
# -----------------------------------------------------------
printf "%-40s" "5. Whisper model"
if [ -f "$WHISPER_MODEL" ]; then
    SIZE=$(du -h "$WHISPER_MODEL" | awk '{print $1}')
    echo -e "[$PASS] $SIZE — $(basename "$WHISPER_MODEL")"
else
    echo -e "[$FAIL] Not found at $WHISPER_MODEL"
    exit 1
fi

# -----------------------------------------------------------
# 6. Record test audio
# -----------------------------------------------------------
echo ""
echo "-----------------------------------------"
echo "  Recording ${RECORD_SECONDS}s of audio..."
echo "  Speak clearly into your mic NOW!"
echo "-----------------------------------------"
echo ""

ffmpeg -y \
    -f pulse -i "$MIC" \
    -ar 16000 -ac 1 -c:a pcm_s16le \
    -t "$RECORD_SECONDS" \
    "$TEST_WAV" 2>/dev/null

printf "%-40s" "6. Audio recorded"
if [ -f "$TEST_WAV" ]; then
    SIZE=$(du -h "$TEST_WAV" | awk '{print $1}')
    DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$TEST_WAV" 2>/dev/null | cut -d. -f1)
    echo -e "[$PASS] ${DURATION}s, ${SIZE}"
else
    echo -e "[$FAIL] Recording failed"
    exit 1
fi

# -----------------------------------------------------------
# 7. Check audio isn't silent
# -----------------------------------------------------------
printf "%-40s" "7. Audio has signal (not silent)"
VOLUME=$(ffmpeg -i "$TEST_WAV" -af "volumedetect" -f null /dev/null 2>&1 | grep mean_volume | awk '{print $5}')
if [ -n "$VOLUME" ]; then
    # mean_volume is negative dB; silence is around -91
    VOLUME_INT=${VOLUME%.*}
    if [ "$VOLUME_INT" -gt -60 ]; then
        echo -e "[$PASS] ${VOLUME} dB"
    else
        echo -e "[$WARN] ${VOLUME} dB — very quiet, check mic input level"
    fi
else
    echo -e "[$WARN] Could not measure volume"
fi

# -----------------------------------------------------------
# 8. Transcribe
# -----------------------------------------------------------
echo ""
echo "-----------------------------------------"
echo "  Transcribing with Whisper (small)..."
echo "-----------------------------------------"
echo ""

TRANSCRIPT=$("$WHISPER_BIN" -m "$WHISPER_MODEL" -f "$TEST_WAV" -l en --no-timestamps 2>/dev/null || true)

printf "%-40s" "8. Transcription"
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
