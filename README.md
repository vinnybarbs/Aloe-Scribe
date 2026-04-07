# 🌿 Aloe Scribe

Local meeting transcription for Linux and Mac.
Records mic + system audio → transcribes with Whisper → syncs to SharePoint.

## How it works

1. Aloe Scribe watches your calendar via an iCal URL
2. 4 minutes before a meeting, a notification prompts: **Start recording / Skip**
3. You click Start — it records silently in the background
4. When the meeting ends (or you click Stop), Whisper transcribes locally
5. The Markdown transcript saves to `~/meetings/` and syncs to SharePoint via rclone

## Quick start

```bash
# 1. Clone / download the project
cd aloe-scribe

# 2. Run the installer
bash scripts/install.sh

# 3. Edit config — add your iCal URL
nano config/config.toml

# 4. Start
systemctl --user start aloe-scribe

# Or run directly to test
python3 src/main.py
```

## Getting your iCal URL

**Google Calendar**
Settings → [your calendar] → Integrate calendar → *Secret address in iCal format*

**Outlook / M365**
Settings → View all Outlook settings → Calendar → Shared calendars → Publish → copy the ICS link

## rclone SharePoint setup

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

## Dependencies

- `ffmpeg` — audio capture
- `whisper.cpp` — local transcription (installed by install.sh)
- `rclone` — SharePoint sync
- `pystray` — system tray
- `icalendar` + `requests` — calendar polling

## Output format

Each transcript is a Markdown file:

```
~/meetings/2026-04-06-1000-weekly-sync.md
```

```markdown
# Weekly Sync
**Date:** April 6, 2026
**Time:** 10:00 AM

---

## Full Transcript

`00:00` Hey, so today we need to...
`00:14` Right, and the deadline is...
```

## Logs

```bash
journalctl --user -u aloe-scribe -f
```

## Project structure

```
aloe-scribe/
├── config/
│   └── config.toml       ← your settings
├── src/
│   ├── main.py           ← entry point + orchestration
│   ├── calendar_watcher.py
│   ├── recorder.py
│   ├── transcriber.py
│   ├── syncer.py
│   └── tray.py
├── scripts/
│   └── install.sh
└── requirements.txt
```
