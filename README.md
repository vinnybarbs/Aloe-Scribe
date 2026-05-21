# Aloe Scribe

Local meeting transcription for macOS and Linux.
Records mic + system audio, transcribes locally with Parakeet TDT (or Whisper), saves Markdown transcripts.

On macOS, system audio is captured natively via **ScreenCaptureKit** — no BlackHole / Multi-Output Device setup required.

## How it works

1. Open the Aloe Scribe window and click **Start Recording Now**
2. It records silently in the background (mic + system audio mixed)
3. Click **Stop & Transcribe** — Parakeet TDT runs locally on Apple Silicon
4. The Markdown transcript saves to your chosen folder (default `~/meetings/`)
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
- Homebrew packages (ffmpeg, python@3.12, cmake)
- Python venv with PyQt6, pyobjc, and `parakeet-mlx`
- whisper.cpp built with **Metal GPU acceleration** (fallback transcriber)
- Compiles the Swift `aloe-audio-capture` helper that drives ScreenCaptureKit
- Builds the `.app` via py2app, code-signs both helper and bundle with stable identifiers
- Installs to `/Applications/Aloe Scribe.app`

Once installed, open **Aloe Scribe** from Spotlight (Cmd+Space) or `/Applications`.

**First-run permissions.** On first launch macOS will prompt for:
- **Microphone** — required, even if you only want system audio.
- **Screen Recording** — required for capturing system audio via ScreenCaptureKit. No video is ever captured or written, only audio.

Both grants attach to the unified `Aloe Scribe` identity (one row in *System Settings → Privacy & Security*, not separate entries per binary).

### Audio setup

In the idle screen you'll see:
- **Microphone** dropdown — real input devices, mic auto-detection prefers external mics over built-in.
- **System Audio** dropdown — `On — Capture system audio` (default) or `Off — Mic only (in-person)`.
- **Save transcripts to** with a **Choose…** button — picks the output folder and writes it back to `config.toml`.

No BlackHole, no Multi-Output Device, no Audio MIDI Setup gymnastics.

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

## Transcription backends

Two backends, switchable via `config.toml`'s `[transcriber] backend = "parakeet"` or `"whisper"`.

| Backend | Default? | Strengths | Tradeoffs |
|---|---|---|---|
| **Parakeet TDT v3** (`parakeet-mlx`) | ✅ macOS default | ~15–20× realtime on Apple Silicon. Doesn't hallucinate `"Thank you."` over silence. Better accuracy on technical/IT English. ~700 MB model, downloaded from HuggingFace on first run, cached in `~/.cache/huggingface/`. | English-only. Treats music/non-speech as silence (a feature for meetings, a bug for podcasts). |
| **whisper.cpp** | Linux default, macOS fallback | Multilingual. Mature. Runs on any CPU. | Hallucinates on silence (mitigated with `-mc 0 -sns` flags). Slower. |

To swap: edit `config.toml` and restart the app.

```toml
[transcriber]
backend = "parakeet"                                      # or "whisper"
parakeet_model = "mlx-community/parakeet-tdt-0.6b-v3"
```

### Whisper model (if using the whisper backend)

`large-v3-turbo` (~1.6 GB) is the recommended default — `install-mac.sh` downloads it for you. Other options:

| Model | Size | Speed | Notes |
|---|---|---|---|
| `small` | 488 MB | ~16× realtime | Fine for clear single speakers |
| `medium.en` | ~1.4 GB | ~6× realtime | English-only, big jump on technical speech |
| `large-v3-turbo` | ~1.6 GB | ~3–4× realtime | **Default** — near-large accuracy |
| `large-v3` | ~3.1 GB | ~1.5–2× realtime | Best accuracy, biggest disk |

After downloading, set `model_path` in `config/config.toml`.

## Configuration

Edit `config/config.toml`:

```toml
[audio]
# Leave blank to auto-detect (recommended). The macOS UI dropdowns
# persist your selection back to this file.
mic_source = ""
system_source = ""

[transcriber]
backend = "parakeet"   # or "whisper"
parakeet_model = "mlx-community/parakeet-tdt-0.6b-v3"

[whisper]
# Used when backend = "whisper" (also the macOS fallback).
binary_path = "~/whisper.cpp/build/bin/whisper-cli"
model_path  = "~/whisper.cpp/models/ggml-large-v3-turbo.bin"

[output]
local_dir = "~/meetings"   # or change via the Choose… button in the app

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
│   └── config.toml             # your settings
├── src/
│   ├── main.py                 # entry point + orchestration
│   ├── ui.py                   # Linux GTK + AppIndicator3 UI
│   ├── ui_mac.py               # macOS PyQt6 UI
│   ├── native_tray.py          # macOS NSStatusItem (menu-bar leaf icon)
│   ├── recorder.py             # Linux audio (PulseAudio)
│   ├── recorder_mac.py         # macOS recorder driver (spawns audio helper)
│   ├── transcriber.py          # whisper.cpp wrapper
│   ├── transcriber_parakeet.py # parakeet-mlx wrapper (macOS default)
│   ├── audio_meter.py          # mic VU bar reader
│   ├── meeting.py              # tiny dataclass for recording label
│   ├── syncer.py               # rclone SharePoint sync
│   └── notifications.py        # NSUserNotification + notify-send
├── tools/
│   └── aloe-audio-capture/
│       └── main.swift          # ScreenCaptureKit system-audio helper
├── bin/                        # compiled helper (gitignored)
│   └── aloe-audio-capture
├── scripts/
│   ├── install.sh              # Linux installer
│   ├── install-mac.sh          # macOS installer (one-shot setup)
│   ├── build-app.sh            # rebuild the macOS .app after code changes
│   ├── build-helper.sh         # rebuild only the Swift audio helper
│   ├── transcribe_wav.py       # recover orphan WAVs after a crash
│   ├── health-check.sh         # Linux audio/transcription test
│   └── health-check-mac.sh     # macOS audio/transcription test
├── assets/
│   └── icon.png                # aloe leaf icon
├── setup.py                    # py2app config for macOS .app bundle
├── requirements.txt            # Linux Python dependencies
└── requirements-mac.txt        # macOS Python dependencies
```

## Rebuilding after a code change (macOS)

For Python-only changes you can just relaunch the app — the bundle's Python
loads the live source from your venv at runtime.

For Swift-helper changes or to fully re-freeze:

```bash
bash scripts/build-app.sh
```

That recompiles `bin/aloe-audio-capture`, re-runs py2app, re-signs everything with the stable ad-hoc identifiers (`com.aloescribe.app`, `com.aloescribe.audio-capture`), and installs the result to `/Applications`.

## Logs

```bash
# macOS — Python-side
cat /tmp/aloe-scribe.log

# macOS — Swift audio-capture helper stderr (per-recording blocks)
cat /tmp/aloe-helper.log

# Linux
journalctl --user -u aloe-scribe -f
```
