# Aloe Scribe

Local meeting transcription for Linux and macOS.
Records mic + system audio, transcribes with Whisper, saves Markdown transcripts.

## How it works

1. Open the Aloe Scribe window and click **Start Recording Now**
2. It records silently in the background (mic + system audio mixed)
3. Click **Stop & Transcribe** — Whisper runs locally
4. The Markdown transcript saves to `~/meetings/`
5. Optionally syncs to SharePoint via rclone

Recordings auto-stop after the `max_duration_minutes` cap (default 120) so a forgotten session can't run forever.

If the app crashes mid-call and leaves an orphan `.wav` behind, recover it with:

```bash
python3 scripts/transcribe_wav.py ~/meetings/2026-04-17-1127-busy.wav
```

## Install — macOS

```bash
git clone https://github.com/vinnybarbs/Aloe-Scribe.git ~/aloe-scribe
cd ~/aloe-scribe
bash scripts/install-mac.sh
```

The installer handles everything:
- Homebrew packages (ffmpeg, python, cmake)
- Python venv with PyQt6
- whisper.cpp built with **Metal GPU acceleration**
- Native `.app` bundle installed to `/Applications`

Once installed, open **Aloe Scribe** from Spotlight (Cmd+Space) or `/Applications`.

### Audio setup

Aloe Scribe auto-detects your microphone each time you start recording. It skips virtual devices (BlackHole, Teams Audio, Zoom) and prefers external mics (USB, Bluetooth) over the built-in MacBook mic.

Switch to AirPods or plug in a USB mic at any time — it picks up the change automatically.

**Optional — system audio capture:** To also capture what others say on calls (not just your mic), install [BlackHole](https://existential.audio/blackhole/):
```bash
brew install --cask blackhole-2ch
```
Then create a Multi-Output Device in Audio MIDI Setup. Note: this disables system volume control, so mic-only recording is recommended for most users.

## Install — Linux

```bash
git clone https://github.com/vinnybarbs/Aloe-Scribe.git ~/aloe-scribe
cd ~/aloe-scribe
bash scripts/install.sh
```

The app runs as a systemd user service with a GTK window and AppIndicator3 tray icon.

```bash
# Start / stop
systemctl --user start aloe-scribe
systemctl --user stop aloe-scribe

# Logs
journalctl --user -u aloe-scribe -f
```

## Whisper model

The default config points at the `large-v3-turbo` model (~1.6 GB). Download it once with:

```bash
cd ~/whisper.cpp
bash models/download-ggml-model.sh large-v3-turbo
```

Tradeoffs (pure-CPU on a modern desktop):

| Model | Size | Speed | Notes |
|---|---|---|---|
| `small` | 488 MB | ~16x realtime | Older default — fine for clear single speakers |
| `medium.en` | ~1.4 GB | ~6x realtime | English-only, big jump on technical speech |
| `large-v3-turbo` | ~1.6 GB | ~3–4x realtime | **Default** — near-large accuracy |
| `large-v3` | ~3.1 GB | ~1.5–2x realtime | Best accuracy, biggest disk |

After downloading, set `model` and `model_path` in `config/config.toml`. (On Linux, `config/config.toml` is git-tracked but flagged `assume-unchanged` so per-machine values stay local — pull updates from the README, then edit the file by hand.)

## Configuration

Edit `config/config.toml`:

```toml
[audio]
# Leave blank to auto-detect (recommended)
mic_source = ""
system_source = ""

[whisper]
# Model: tiny, base, small, medium, large-v3, large-v3-turbo
# "large-v3-turbo" is the recommended default — near-large accuracy,
# ~3-4x realtime on a modern CPU. Falls back to "small" on slower hardware.
model = "large-v3-turbo"
model_path = "~/whisper.cpp/models/ggml-large-v3-turbo.bin"

[app]
# Hard cap on recording length — auto-stops + transcribes after this many minutes
max_duration_minutes = 120
```

## Health check

Test that your mic, ffmpeg, and Whisper are all working:

```bash
# macOS
bash scripts/health-check-mac.sh

# Linux
bash scripts/health-check.sh
```

Records a short clip, checks audio levels, and transcribes it.

## SharePoint sync (optional)

```bash
rclone config
# Storage type: Microsoft OneDrive
# Drive type: SharePoint document library
# Remote name: sharepoint
```

Then in `config.toml`:
```toml
[sync]
enabled = true
rclone_remote = "sharepoint:Documents/Meetings"
```

## Output format

Transcripts are saved as Markdown in `~/meetings/`:

```
~/meetings/2026-04-07-1400-weekly-sync.md
```

```markdown
# Weekly Sync
**Date:** April 7, 2026
**Time:** 02:00 PM
**Transcribed by:** Aloe Scribe

---

## Full Transcript

`00:00` Hey, so today we need to discuss the timeline...
`00:14` Right, and the deadline for the API is next Friday...

---

_Transcript generated 2026-04-07 14:45 · Aloe Scribe_
```

## Project structure

```
aloe-scribe/
├── config/
│   └── config.toml            # your settings
├── src/
│   ├── main.py                # entry point + orchestration
│   ├── ui.py                  # Linux GTK + AppIndicator3 UI
│   ├── ui_mac.py              # macOS PyQt6 UI
│   ├── recorder.py            # Linux audio (PulseAudio)
│   ├── recorder_mac.py        # macOS audio (avfoundation)
│   ├── transcriber.py         # whisper.cpp wrapper
│   ├── meeting.py             # tiny dataclass for recording label
│   ├── syncer.py              # rclone SharePoint sync
│   └── notifications.py       # cross-platform notifications
├── scripts/
│   ├── install.sh             # Linux installer
│   ├── install-mac.sh         # macOS installer (builds .app)
│   ├── transcribe_wav.py      # recover orphan WAVs after a crash
│   ├── health-check.sh        # Linux audio/transcription test
│   └── health-check-mac.sh    # macOS audio/transcription test
├── assets/
│   └── icon.png               # aloe leaf icon
├── setup.py                   # py2app config for macOS .app bundle
├── requirements.txt           # Linux Python dependencies
└── requirements-mac.txt       # macOS Python dependencies
```

## Logs

```bash
# macOS
cat /tmp/aloe-scribe.log

# Linux
journalctl --user -u aloe-scribe -f
```
