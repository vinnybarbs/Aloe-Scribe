# Aloe Scribe

Local meeting transcription for Linux and macOS.
Records mic + system audio, transcribes with Whisper, saves Markdown transcripts.

## How it works

1. Aloe Scribe watches your calendar via an iCal URL
2. 4 minutes before a meeting, a notification prompts: **Start recording / Skip**
3. You click Start — it records silently in the background
4. When the meeting ends (or you click Stop), Whisper transcribes locally
5. The Markdown transcript saves to `~/meetings/`
6. Optionally syncs to SharePoint via rclone

You can also click **Start Recording Now** at any time for manual recordings.

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

## Configuration

Edit `config/config.toml`:

```toml
[calendar]
# Paste your iCal URL here (see below for how to get it)
ical_url = ""

[audio]
# Leave blank to auto-detect (recommended)
mic_source = ""
system_source = ""

[whisper]
# Model: tiny, base, small, medium, large-v3
# "small" is the best balance of speed and accuracy
model = "small"
```

### Getting your iCal URL

**Google Calendar:**
Settings > [your calendar] > Integrate calendar > *Secret address in iCal format*

**Outlook / Microsoft 365:**
Settings > View all Outlook settings > Calendar > Shared calendars > Publish > copy the ICS link

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
│   ├── calendar_watcher.py    # iCal polling
│   ├── syncer.py              # rclone SharePoint sync
│   └── notifications.py       # cross-platform notifications
├── scripts/
│   ├── install.sh             # Linux installer
│   ├── install-mac.sh         # macOS installer (builds .app)
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
