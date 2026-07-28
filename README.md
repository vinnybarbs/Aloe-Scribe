# Aloe Scribe

Local meeting transcription for macOS, Windows, and Linux. It records your mic plus system audio, transcribes on your machine, and saves a Markdown transcript. Audio never leaves the machine.

macOS transcribes with Parakeet TDT and captures system audio natively with ScreenCaptureKit, so there is no BlackHole or Multi-Output Device setup. Windows transcribes with faster-whisper and captures system audio with WASAPI loopback, built into Windows 10 and 11, so it needs no virtual cable. Linux uses whisper.cpp.

> Repo layout. This `main` branch is the desktop app for macOS, Windows, and Linux (`src/`, `scripts/`, `tools/`). The platform code is picked at runtime, so the Windows files never load on macOS and the reverse. The iPhone app (Xcode, SwiftUI) lives only on the `ios` branch (`ios/`). Do all desktop work on `main`. Run `git checkout ios` only for the phone app. `main` never contains `ios/`.

## How it works

1. Open Aloe Scribe and click Start recording.
2. It records in the background, mic and system audio mixed.
3. A live transcript appears on screen as you talk, so you can see it is working.
4. Click Stop. A clean full transcript saves to your folder (default `~/meetings/`).
5. It optionally syncs to SharePoint with rclone.

The transcript streams during the recording and is checkpointed to the `.md` file every 30 seconds, so a crash mid-call does not lose what was already said. At Stop, one clean full pass over the whole recording overwrites the file as the final version.

Recordings auto-stop after the `max_duration_minutes` cap (default 120), so a forgotten session cannot run forever.

If the app crashes and leaves an orphan `.wav` behind, recover it:

```bash
python3 scripts/transcribe_wav.py ~/meetings/2026-04-17-1127-busy.wav
```

## Install, macOS

New machine. One command sets up the Python env, the dependencies, the Parakeet model (downloaded from GitHub, not Hugging Face), and the app:

```bash
git clone https://github.com/vinnybarbs/Aloe-Scribe.git ~/aloe-scribe
cd ~/aloe-scribe
bash scripts/install-mac.sh
```

> Already have Aloe Scribe installed? Do not re-run the installer, it resets your settings. Use the updater instead. See [Updating to the latest version](#updating-to-the-latest-version-macos).

The installer does the following:
- Installs Homebrew packages (ffmpeg, python@3.12, cmake).
- Builds a Python venv with PyQt6, pyobjc, and hash-pinned dependencies (`--require-hashes`).
- Downloads the Parakeet model from this repo's GitHub Release and verifies the checksum.
- Compiles the Swift `aloe-audio-capture` helper that drives ScreenCaptureKit.
- Builds the `.app` with py2app and signs it with a stable self-signed identity (created by `scripts/create-signing-cert.sh`), so macOS keeps your Screen Recording and Microphone permissions across updates instead of revoking them on every rebuild.
- Installs to `/Applications/Aloe Scribe.app`.

whisper.cpp is not installed by default. Parakeet is the macOS default and does not need it. If you want whisper as a fallback (multilingual, or non-Apple-Silicon hardware), run:

```bash
INSTALL_WHISPER=1 bash scripts/install-mac.sh
```

That builds whisper.cpp with Metal acceleration and downloads `large-v3-turbo` (about 1.6 GB).

Once installed, open Aloe Scribe from Spotlight (Cmd+Space) or `/Applications`.

### Transcription model, no Hugging Face required

The Parakeet weights (about 2.3 GB) are hosted as a GitHub Release on this repo, not pulled from Hugging Face. `install-mac.sh` downloads them from the release, reassembles them, verifies the checksum, drops them in `models/parakeet-tdt-0.6b-v3/`, and points the app at that local path. The app loads the model from disk and forces Hugging Face offline at runtime, so it never contacts `huggingface.co`. Installs work where that host is blocked.

Maintainers publish or refresh the model release with the model in the local HF cache or a local dir:

```bash
bash scripts/publish-model.sh            # uses the HF-cache copy
bash scripts/publish-model.sh /path/dir  # or an explicit model dir
```

That splits `model.safetensors` into parts under 2 GiB (the GitHub asset limit), writes a `SHA256SUMS` manifest, and uploads everything to the `model-parakeet-tdt-0.6b-v3` release. Model license: CC-BY-4.0 (NVIDIA).

## Updating to the latest version (macOS)

```bash
cd ~/aloe-scribe
bash scripts/update-mac.sh
```

That pulls the newest code, keeps your settings (calendar URL, mic, output folder live inside the installed app bundle), creates the stable signing identity if it is missing, and rebuilds and reinstalls the app. A relaunch alone is not enough. The app code is frozen into the bundle, so an update has to rebuild it.

> First update only. If you installed before settings were untracked, your local `config/config.toml` may still be tracked by git and block the pull. Stash it once. Your live settings are safe inside the installed app and get restored automatically. After that it is just `update-mac.sh`:
> ```bash
> cd ~/aloe-scribe
> git stash
> bash scripts/update-mac.sh
> ```

First-run permissions. On first launch macOS prompts for two things:
- Microphone, required even if you only want system audio.
- Screen Recording, required to capture system audio through ScreenCaptureKit. No video is ever captured or written, only audio.

Both grants attach to one `Aloe Scribe` identity, one row in System Settings, Privacy and Security, not separate entries per binary. With the stable signing identity you grant these once. Updates do not make you re-grant.

> Close background media before recording. ScreenCaptureKit captures your Mac's entire system-audio mix, so a YouTube tab, music, or a video editor playing in the background gets recorded on top of your meeting and can bury the speech. The idle screen shows a System audio level meter. Glance at it before you hit record.

### Audio setup

The idle screen has three controls:
- Microphone dropdown, the real input devices. Auto-detect prefers an external mic over the built-in one.
- System audio dropdown, "On, capture system audio" (default) or "Off, mic only (in-person)".
- Save transcripts to, with a Choose button that sets the output folder and writes it back to `config.toml`.

No BlackHole, no Multi-Output Device, no Audio MIDI Setup.

## Install, Linux

```bash
git clone https://github.com/vinnybarbs/Aloe-Scribe.git ~/aloe-scribe
cd ~/aloe-scribe
bash scripts/install.sh
```

The app runs as a systemd user service with a GTK window and an AppIndicator3 tray icon.

```bash
# Start and stop
systemctl --user start aloe-scribe
systemctl --user stop aloe-scribe

# Logs
journalctl --user -u aloe-scribe -f
```

## Install, Windows

Windows support is new. The dependency install, the model download, the transcription, and the `.exe` build are all verified on Windows. Live audio capture and the on-screen window are still being confirmed on hardware, so treat this as a preview.

Running from source is the quickest way to test. In PowerShell:

```powershell
git clone https://github.com/vinnybarbs/Aloe-Scribe.git
cd Aloe-Scribe
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
powershell -ExecutionPolicy Bypass -File scripts\run-windows.ps1
```

`install-windows.ps1` creates a Python venv, installs the Windows dependencies, downloads the faster-whisper model from this repo's GitHub Release (not Hugging Face), points the app at the local copy, and sets your transcript folder to `%USERPROFILE%\meetings`. `run-windows.ps1` launches the app.

Update later with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\update-windows.ps1
```

To build a standalone `.exe` instead of running from source:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
```

That produces `dist\Aloe Scribe\Aloe Scribe.exe`.

### Windows installer

For a real double-click installer with a wizard, Start menu and desktop shortcuts, and an uninstaller, build `AloeScribeSetup.exe`. It needs [Inno Setup 6](https://jrsoftware.org/isdl.php), or `choco install innosetup`.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-installer-windows.ps1
```

The result is `installer\Output\AloeScribeSetup.exe`. It is a per-user install, so it does not prompt for admin, and it bundles the transcription model, so the installed app works offline with nothing to download. The installer is unsigned for now, so on first launch Windows SmartScreen shows "More info" then "Run anyway". A signed build is a later step if you distribute it widely.

A few Windows specifics:
- System audio uses WASAPI loopback, built into Windows 10 and 11, so there is nothing to install for it.
- faster-whisper uses an NVIDIA GPU when one is present and the CPU otherwise, detected automatically.
- Windows transcribes when you press Stop. faster-whisper has no real-time mode, so the on-screen preview is a live sample. Every few seconds it re-transcribes the recent audio so you can see the recording is working. The saved transcript is the full pass at Stop.
- The log is at `%TEMP%\aloe-scribe.log`.

## Transcription backends

Three backends, set in `config.toml` with `[transcriber] backend`. Use `parakeet` on macOS, `faster_whisper` on Windows, or `whisper` for whisper.cpp.

| Backend | Default | Strengths | Tradeoffs |
|---|---|---|---|
| Parakeet TDT v3 (`parakeet-mlx`) | macOS default | 15 to 20 times realtime on Apple Silicon, plus cache-aware streaming for the live transcript. Does not hallucinate "Thank you." over silence. Good accuracy on technical English. Model ships from this repo's GitHub Release, no Hugging Face. | English only. Apple Silicon only. Treats music and non-speech as silence, which suits meetings. |
| faster-whisper (CTranslate2) | Windows default | Cross-platform. CPU or NVIDIA CUDA, detected automatically. Loads from a local folder. Model ships from this repo's GitHub Release, no Hugging Face. | No real-time mode. The live view is a rolling sample, and the saved transcript is the full pass at Stop. |
| whisper.cpp | Linux default, macOS fallback | Multilingual. Mature. Runs on any CPU. | Hallucinates on silence, reduced with `-mc 0 -sns`. Slower. |

To swap, edit `config.toml` and restart the app:

```toml
[transcriber]
backend = "parakeet"   # or "whisper"
parakeet_model = "mlx-community/parakeet-tdt-0.6b-v3"
```

### Whisper model (whisper backend only)

`large-v3-turbo` (about 1.6 GB) is the default and `install-mac.sh` downloads it for you. Other options:

| Model | Size | Speed | Notes |
|---|---|---|---|
| `small` | 488 MB | about 16x realtime | Fine for clear single speakers |
| `medium.en` | about 1.4 GB | about 6x realtime | English only, a jump on technical speech |
| `large-v3-turbo` | about 1.6 GB | about 3 to 4x realtime | Default, near-large accuracy |
| `large-v3` | about 3.1 GB | about 1.5 to 2x realtime | Best accuracy, biggest disk |

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
# parakeet (macOS), faster_whisper (Windows), or whisper (whisper.cpp)
backend = "parakeet"
parakeet_model = "mlx-community/parakeet-tdt-0.6b-v3"

# Label transcript lines by speaker (see Speaker labels). false = old
# unlabeled mixed-mono behavior.
speaker_labels = true

# Used when backend = "faster_whisper" (Windows). install-windows.ps1 sets the
# model to the local folder it downloads, and the device to auto (NVIDIA GPU
# when present, else CPU).
faster_whisper_model = "faster-distil-whisper-large-v3"
faster_whisper_device = "auto"

[whisper]
# Used when backend = "whisper" (also the macOS fallback).
binary_path = "~/whisper.cpp/build/bin/whisper-cli"
model_path  = "~/whisper.cpp/models/ggml-large-v3-turbo.bin"

[output]
local_dir = "~/meetings"   # or change with the Choose button in the app

[app]
# Hard cap on recording length. Auto-stops and transcribes after this many minutes.
max_duration_minutes = 120
```

## Health check

Test that your mic, ffmpeg, and the transcriber work:

```bash
# macOS
bash scripts/health-check-mac.sh

# Linux
bash scripts/health-check.sh
```

It records a short clip, checks audio levels, and transcribes it.

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

The transcript is a handoff file. A downstream agent reads it to enrich with calendar and email context and to summarize. So it carries data, not formatting: a machine-readable YAML header, then the timestamped transcript. No prose.

```
~/meetings/2026-04-07-1400-weekly-sync.md
```

```markdown
---
title: "Weekly sync"
date: 2026-04-07T14:00:00-06:00
end: 2026-04-07T14:32:00-06:00
duration_min: 32
source: aloe-scribe-mac
speakers: [M1, R1, R2]
speaker_key: "M* = voices on the local microphone ..."
---

[00:00] M1: Hey, so today we need to discuss the timeline.
[00:14] R1: Right, and the deadline for the API is next Friday.
[00:21] R2: I can have the draft ready by Wednesday.
```

The header carries the meeting as a time window, so the agent can match the calendar event and infer attendees from there. Desktop and iOS write the same header fields. The speaker fields appear only on desktop recordings that captured both mic and system audio (see Speaker labels below).

## Speaker labels

When both the mic and system audio are being captured, the recorder keeps them as separate channels instead of mixing them (macOS: stereo WAV from the Swift helper. Windows: stereo merge of the two WASAPI tracks). That split gives every transcript line a speaker label with no guesswork about which side of the call it came from:

- `M1, M2, ...` are voices heard on the local microphone. This is the in-room side. It is often the machine's owner, but not always, so nothing is ever labeled with a name. A laptop recording a conference room will produce several M speakers.
- `R1, R2, ...` are voices heard on system audio, meaning the remote participants.

Telling voices apart within a side uses offline speaker diarization (sherpa-onnx, pyannote segmentation plus CAM++ embeddings, about 36 MB of models downloaded once to `~/.cache/aloe-scribe/diarization/`). If sherpa-onnx or its models are unavailable, the transcript still gets channel-level labels: everything on the mic is `M1`, everything remote is `R1`, and the header says so in `speaker_note`.

The labels are deliberately anonymous. The downstream summary agent maps them to names using the matched calendar event and conversational context ("thanks, Priya" said by M1 right after R2 finishes is a strong hint). The `speaker_key` header field explains the scheme to the agent so it does not have to guess.

Set `speaker_labels = false` under `[transcriber]` in config.toml to restore the old behavior (mixed mono recording, no labels). Recordings with a single source (mic-only or system-only) stay mono and unlabeled either way.

## Project structure

```
aloe-scribe/
├── config/
│   └── config.toml             # your settings
├── src/
│   ├── main.py                 # entry point and orchestration
│   ├── ui.py                   # Linux GTK and AppIndicator3 UI
│   ├── ui_mac.py               # macOS PyQt6 UI
│   ├── ui_windows.py           # Windows UI (reuses ui_mac, Qt system tray)
│   ├── native_tray.py          # macOS NSStatusItem (menu-bar leaf icon)
│   ├── recorder.py             # Linux audio (PulseAudio)
│   ├── recorder_mac.py         # macOS recorder driver (spawns audio helper)
│   ├── recorder_windows.py     # Windows audio (WASAPI loopback + mic)
│   ├── transcriber.py          # whisper.cpp wrapper
│   ├── transcriber_parakeet.py # parakeet-mlx wrapper (macOS default, streaming)
│   ├── transcriber_faster_whisper.py # faster-whisper wrapper (Windows default)
│   ├── audio_meter.py          # mic and system VU bar reader
│   ├── frontmatter.py          # transcript YAML header
│   ├── meeting.py              # tiny dataclass for the recording label
│   ├── syncer.py               # rclone SharePoint sync
│   └── notifications.py        # NSUserNotification and notify-send
├── tools/
│   └── aloe-audio-capture/
│       └── main.swift          # ScreenCaptureKit system-audio helper
├── bin/                        # compiled helper (gitignored)
│   └── aloe-audio-capture
├── scripts/
│   ├── install.sh              # Linux installer
│   ├── install-mac.sh          # macOS installer (one-shot setup)
│   ├── install-windows.ps1     # Windows installer (venv, deps, model)
│   ├── run-windows.ps1         # launch the Windows app from source
│   ├── update-mac.sh           # pull, rebuild, reinstall, keep settings
│   ├── update-windows.ps1      # Windows: pull, refresh deps, keep model
│   ├── build-app.sh            # rebuild the macOS .app after a code change
│   ├── build-windows.ps1       # build the Windows .exe with PyInstaller
│   ├── build-installer-windows.ps1 # build AloeScribeSetup.exe (Inno Setup)
│   ├── build-helper.sh         # rebuild only the Swift audio helper
│   ├── create-signing-cert.sh  # one-time stable signing identity
│   ├── fetch-model.sh          # download the macOS model from the GitHub Release
│   ├── fetch-model-windows.ps1 # download the Windows model from the GitHub Release
│   ├── publish-model.sh        # maintainers: publish the macOS model release
│   ├── publish-model-windows.sh # maintainers: publish the Windows model release
│   ├── transcribe_wav.py       # recover orphan WAVs after a crash
│   ├── health-check.sh         # Linux audio and transcription test
│   └── health-check-mac.sh     # macOS audio and transcription test
├── installer/
│   └── aloe-scribe.iss         # Inno Setup script for AloeScribeSetup.exe
├── tests/
│   ├── sample.wav              # short clip used by Windows CI
│   └── win_smoke.py            # Windows transcription smoke test
├── .github/workflows/
│   └── windows-ci.yml          # builds and tests the Windows port on Windows runners
├── assets/
│   └── icon.png                # aloe leaf icon
├── setup.py                    # py2app config for the macOS .app bundle
├── aloe-scribe-windows.spec    # PyInstaller config for the Windows .exe
├── requirements.txt            # Linux Python dependencies
├── requirements-mac.txt        # macOS Python dependencies (hash-pinned)
└── requirements-windows.txt    # Windows Python dependencies
```

## Rebuilding after a code change (macOS)

The Python source is frozen into the bundle at build time, so a code change needs a rebuild. Relaunching alone will not pick it up.

```bash
bash scripts/build-app.sh
```

To also pull the latest from GitHub and keep your settings in one step, use `scripts/update-mac.sh`. See [Updating](#updating-to-the-latest-version-macos).

That recompiles `bin/aloe-audio-capture`, re-runs py2app, signs everything with the stable identity, and installs the result to `/Applications`.

## Logs

```bash
# macOS, Python side
cat /tmp/aloe-scribe.log

# macOS, Swift audio-capture helper stderr (per-recording blocks)
cat /tmp/aloe-helper.log

# Linux
journalctl --user -u aloe-scribe -f
```

On Windows the log is at `%TEMP%\aloe-scribe.log`. View it in PowerShell with `Get-Content $env:TEMP\aloe-scribe.log`.
