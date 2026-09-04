# Aloe Scribe

The private meeting notepad. Aloe Scribe records your mic plus system audio, transcribes on your machine, labels who said what, writes a summary with a small local model, and saves it all as one Markdown file. Nothing leaves the machine. There is no account, no server, and no vendor reading your meetings.

Website and downloads: [aloescribe.ai](https://aloescribe.ai). Security posture: [aloescribe.ai/security](https://aloescribe.ai/security.html) and [SECURITY.md](SECURITY.md).

macOS transcribes with Parakeet TDT and captures system audio natively with ScreenCaptureKit, so there is no BlackHole or Multi-Output Device setup. Windows transcribes with faster-whisper and captures system audio with WASAPI loopback, built into Windows 10 and 11, so it needs no virtual cable. Linux uses whisper.cpp.

> Repo layout. This `main` branch is the desktop app for macOS, Windows, and Linux (`src/`, `scripts/`, `tools/`). The platform code is picked at runtime, so the Windows files never load on macOS and the reverse. The iPhone app (Xcode, SwiftUI) lives only on the `ios` branch (`ios/`). Do all desktop work on `main`. Run `git checkout ios` only for the phone app. `main` never contains `ios/`.

## Download

- **macOS**: a signed and notarized DMG from [aloescribe.ai](https://aloescribe.ai) or the latest `mac-v*` [release](https://github.com/vinnybarbs/Aloe-Scribe/releases). Drag the app to Applications and launch it from there, not from the mounted DMG (the DMG is read-only, so settings cannot save). The DMG currently expects the models and Python environment that the install script provides, so on a brand-new machine run the install command below once first. A self-contained first run is in progress.
- **Windows**: `AloeScribeSetup.exe` from the `windows-v*` release. Per-user install, no admin prompt, model bundled, works offline. The installer is unsigned for now, so SmartScreen shows "More info" then "Run anyway".
- **From source**: see the install sections below.

## How it works

1. Open Aloe Scribe and pick a folder for your transcripts. The app ships with no folder chosen on purpose, so nothing is ever written somewhere you did not pick. Change it any time with the Choose button and everything follows.
2. Click Start recording. It records in the background, mic and system audio as separate channels.
3. A live transcript appears on screen as you talk, so you can see it is working. Type attendee names in the notes window and click a name while that person speaks to tag their voice.
4. Click Stop. A clean full transcript saves to your folder, speakers labeled, then the summary and action items are written by a local model about ten seconds later.
5. It optionally syncs to SharePoint with rclone.

The transcript streams during the recording and is checkpointed to the `.md` file every 30 seconds, so a crash mid-call does not lose what was already said. At Stop, one clean full pass over the whole recording overwrites the file as the final version.

Recordings auto-stop after the `max_duration_minutes` cap (default 120), so a forgotten session cannot run forever.

If the app crashes and leaves an orphan `.wav` behind, recover it:

```bash
python3 scripts/transcribe_wav.py ~/meetings/2026-04-17-1127-busy.wav
```

## System requirements

Everything runs on the endpoint, so the hardware matters. Peak memory
figures below are measured, and the stages are staggered by design
(transcription during the meeting, diarization at stop, the summary only
after the transcript is saved), so the realistic concurrent peak is about
4 GB, not the sum of the parts.

**macOS**

- Apple Silicon (M1 or later) is a hard requirement. The transcriber and
  summarizer run on MLX, which does not exist for Intel Macs.
- macOS 13 minimum (system-audio capture), macOS 14 recommended (echo
  cancellation ducking control).
- RAM: 8 GB minimum, 16 GB recommended. Transcription peaks at 1.2 GB,
  diarization at 0.8 GB, and the summary model at 3 to 4 GB, alongside
  whatever the meeting app and browser are using.
- Disk: 12 GB free (the two bundled models are 5.2 GB of that).
- Any M-series chip is fast enough. Newer chips shorten the
  stop-to-transcript wait from tens of seconds toward seconds.

**Windows**

- 64-bit Windows 10 or 11.
- x64 CPU with AVX2 (roughly Intel 2013 / AMD 2015 or newer), 4 cores
  minimum, 8 recommended. An NVIDIA GPU is optional and speeds
  transcription up considerably.
- RAM: 8 GB minimum, 16 GB recommended.
- Disk: 6 GB free.
- Windows currently ships without the summary block, echo cancellation,
  and voice profiles (those depend on Apple-only runtimes).

## Install, macOS

New machine. One command clones the repo, sets up the Python env, the dependencies, the Parakeet model (downloaded from GitHub, not Hugging Face), and builds the app into `/Applications`:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/vinnybarbs/Aloe-Scribe/main/scripts/get-mac.sh)"
```

Requirements: Apple Silicon Mac on macOS 13 or newer (Intel works with the whisper.cpp fallback, slower). If Xcode Command Line Tools are missing, the installer triggers the install dialog and asks you to re-run afterward.

Already have the repo cloned? The same flow works from inside it:

```bash
bash scripts/install-mac.sh
```

> Already have Aloe Scribe installed? Do not re-run the installer. Use the in-app Update button or the updater script. See [Updating](#updating-to-the-latest-version-macos).

The installer does the following:
- Installs Homebrew packages (ffmpeg, python@3.12, cmake).
- Builds a Python venv with PySide6, pyobjc, and hash-pinned dependencies (`--require-hashes`).
- Downloads the Parakeet model from this repo's GitHub Release and verifies the checksum.
- Compiles the Swift `aloe-audio-capture` helper that drives ScreenCaptureKit.
- Builds the `.app` with py2app and signs it with a stable identity, so macOS keeps your Screen Recording and Microphone permissions across updates instead of revoking them on every rebuild.
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

Click the Update button in the app. It pulls the newest code, keeps your settings, rebuilds, and relaunches. No Terminal needed.

From a Terminal, the install one-liner is also the updater and can be run from anywhere:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/vinnybarbs/Aloe-Scribe/main/scripts/get-mac.sh)"
```

When it finds an existing install it updates instead of reinstalling. Your settings are safe either way. They live outside the app in `~/Library/Application Support/Aloe Scribe/config.toml`, so updates and reinstalls never touch them. From inside the repo, `bash scripts/update-mac.sh` does the same update directly. A relaunch alone is not enough after a code change, because the app code is frozen into the bundle at build time.

First-run permissions. On first launch macOS prompts for two things:
- Microphone, required even if you only want system audio.
- Screen Recording, required to capture system audio through ScreenCaptureKit. No video is ever captured or written, only audio.

Both grants attach to one `Aloe Scribe` identity, one row in System Settings, Privacy and Security, not separate entries per binary. You grant these once. Updates do not make you re-grant.

> Close background media before recording. ScreenCaptureKit captures your Mac's entire system-audio mix, so a YouTube tab, music, or a video editor playing in the background gets recorded on top of your meeting and can bury the speech. The idle screen shows an audio level meter. Glance at it before you hit record.

### Audio setup

The idle screen has three controls:
- Microphone dropdown, the real input devices. It starts on Select microphone and Start requires a choice, with your saved mic preselected whenever it is connected.
- System audio dropdown, "On, capture system audio" (default) or "Off, mic only (in-person)".
- Save transcripts to, with a Choose button. Until you pick a folder the app will not record, and it shows "No folder chosen yet". Your choice persists and can be changed whenever you like.

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

The double-click installer is the easy path: download `AloeScribeSetup.exe` from the latest `windows-v*` [release](https://github.com/vinnybarbs/Aloe-Scribe/releases). It installs per-user with Start menu and desktop shortcuts, bundles the transcription model, and works offline. Live capture is verified in CI, and on-hardware testing is still in progress, so treat Windows as a preview.

Running from source is the quickest way to develop. In PowerShell:

```powershell
git clone https://github.com/vinnybarbs/Aloe-Scribe.git
cd Aloe-Scribe
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
powershell -ExecutionPolicy Bypass -File scripts\run-windows.ps1
```

`install-windows.ps1` creates a Python venv, installs the Windows dependencies, downloads the faster-whisper model from this repo's GitHub Release (not Hugging Face), and points the app at the local copy. `run-windows.ps1` launches the app.

Update later with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\update-windows.ps1
```

To build the standalone `.exe` and the installer yourself, see [RELEASING.md](RELEASING.md). Short version: `scripts\build-windows.ps1` produces `dist\Aloe Scribe\Aloe Scribe.exe` with PyInstaller, and `scripts\build-installer-windows.ps1` wraps it into `installer\Output\AloeScribeSetup.exe` with [Inno Setup 6](https://jrsoftware.org/isdl.php).

A few Windows specifics:
- System audio uses WASAPI loopback, built into Windows 10 and 11, so there is nothing to install for it.
- faster-whisper uses an NVIDIA GPU when one is present and the CPU otherwise, detected automatically.
- Windows transcribes when you press Stop. faster-whisper has no real-time mode, so the on-screen preview is a live sample. Every few seconds it re-transcribes the recent audio so you can see the recording is working. The saved transcript is the full pass at Stop.
- The log is at `%TEMP%\aloe-scribe.log`.

## Transcription backends

Three backends, set in the config with `[transcriber] backend`. Use `parakeet` on macOS, `faster_whisper` on Windows, or `whisper` for whisper.cpp.

| Backend | Default | Strengths | Tradeoffs |
|---|---|---|---|
| Parakeet TDT v3 (`parakeet-mlx`) | macOS default | 15 to 20 times realtime on Apple Silicon, plus cache-aware streaming for the live transcript. Does not hallucinate "Thank you." over silence. Good accuracy on technical English. Model ships from this repo's GitHub Release, no Hugging Face. | English only. Apple Silicon only. Treats music and non-speech as silence, which suits meetings. |
| faster-whisper (CTranslate2) | Windows default | Cross-platform. CPU or NVIDIA CUDA, detected automatically. Loads from a local folder. Model ships from this repo's GitHub Release, no Hugging Face. | No real-time mode. The live view is a rolling sample, and the saved transcript is the full pass at Stop. |
| whisper.cpp | Linux default, macOS fallback | Multilingual. Mature. Runs on any CPU. | Hallucinates on silence, reduced with `-mc 0 -sns`. Slower. |

To swap, edit the config and restart the app:

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

After downloading, set `model_path` in the config.

## Configuration

Where the config file lives depends on how you run the app:

- Installed app on macOS: `~/Library/Application Support/Aloe Scribe/config.toml`
- Installed app on Windows: `%APPDATA%\Aloe Scribe\config.toml`
- Running from source: `config/config.toml` in the repo

The app creates the file from `config/config.toml.example` on first launch. Builds never ship anyone's real settings, only the template, and the template ships with no output folder set. That is deliberate. See [RELEASING.md](RELEASING.md) for the rules.

The common settings:

```toml
[audio]
# The mic is chosen in the app (dropdown starts on "Select microphone…" and
# Start requires a choice). The selection persists here and is preselected
# whenever that device is connected.
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
# Empty means not chosen yet. Pick your folder with the Choose button in the
# app. The app refuses to record until a folder is chosen.
local_dir = ""

[summarizer]
# The local summary model. Set false to skip the Summary and Action items
# sections. Also togglable in the app.
enabled = true

[app]
# Hard cap on recording length. Auto-stops and transcribes after this many
# minutes. Enforced on wall-clock time, so it works even if the Mac slept
# mid-recording.
max_duration_minutes = 120

# Auto-stop after this many minutes of continuous silence (mic AND system
# audio quiet), so a forgotten recording ends minutes after the meeting does
# instead of hours later at the cap. 0 disables.
silence_timeout_minutes = 10
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

Then in the config:
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
speakers: [Vince, Priya, R2]
attendees: [Vince, Priya, Jordan]
---

# Weekly sync

Tuesday, April 7, 2026 at 14:00 · 32 min
Attendees: Vince, Priya, Jordan
Speakers heard: Vince, Priya, R2

## Summary
Bullets a small local model writes after the transcript lands.

## Action items
Priya: send the revised SOW by Friday.

## Notes
Rough notes typed during the meeting.

## Transcript
[00:00] Vince: Hey, so today we need to discuss the timeline.
[00:14] Priya: Right, the API deadline is next Friday.
```

The document leads with what retrieval agents rank on: a real title (typed in the Meeting Notes window, it also names the file), plain-text metadata, the summary, and your notes, with the raw dialogue demoted to a Transcript section at the end. The Summary and Action items are written by a small local model (Qwen 3.5 4B via MLX) about ten seconds after the transcript lands. Nothing leaves the machine, the model reads your notes and tags so the summary reflects what you flagged, and it can be turned off in the app or the config. The header carries the meeting as a time window, so the agent can match the calendar event and infer attendees from there. Desktop and iOS write the same header fields. The speaker fields appear only on desktop recordings that captured both mic and system audio (see Speaker labels below).

## Speaker labels

When both the mic and system audio are being captured, the recorder keeps them as separate channels instead of mixing them (macOS: stereo WAV from the Swift helper. Windows: stereo merge of the two WASAPI tracks). Each channel is then transcribed separately, so which side of the call a line came from is a structural fact rather than a guess, and two people talking at the same time on opposite sides are both captured instead of colliding in one mono stream. A near-silent channel (a muted mic) is skipped, and mic lines that are just acoustic echo of the remote audio are deduplicated away on setups without echo cancellation. The labels:

- `M1, M2, ...` are voices heard on the local microphone. This is the in-room side. It is often the machine's owner, but not always, so nothing is ever labeled with a name automatically. A laptop recording a conference room will produce several M speakers.
- `R1, R2, ...` are voices heard on system audio, meaning the remote participants.

Telling voices apart within a side uses offline speaker diarization. On Apple Silicon the primary engine is [Senko](https://github.com/narcotic-sh/senko) (CoreML-accelerated, density-based speaker counting, about an hour of audio in under 10 seconds). It was chosen after it matched human-confirmed speaker counts on real meetings where the previous engine over-split. When you tag speakers live, the clustering is constrained by your tags (google's spectralcluster with constraint propagation), so a tag does not just name one line, it anchors that voice everywhere it appears. When Senko is unavailable (Windows, or a failed install) the pipeline falls back to sherpa-onnx (pyannote segmentation plus CAM++ embeddings, about 36 MB of models in `~/.cache/aloe-scribe/diarization/`), and if that is also unavailable the transcript still gets channel-level labels: everything on the mic is `M1`, everything remote is `R1`, and the header says so in `speaker_note`.

Accuracy expectations: the M-versus-R side of a line is reliable because it comes from the channel itself. Splits within a side (R1 versus R2) are statistical and imperfect, especially over compressed meeting audio. Two similar voices can merge, and one person can occasionally split into two labels. Tags fix this: a cluster tagged with a name is that person, and an attendee list plus tags lets the pipeline resolve the leftovers by elimination. Treat an untagged R-number as a strong hint, not ground truth.

Naming speakers happens live, in the Meeting Notes window that opens with each recording. Type attendee names, then click a name while that person is talking. Tagging beats any after-the-fact guessing because you can hear the speaker at tag time, and two clusters tagged with the same name merge automatically. The window also holds a timestamped notepad whose entries are saved into a Notes section of the transcript, and a live transcript pane that only autoscrolls while you are at the bottom. When processing finishes, the pane becomes the editable final transcript with one chip button per speaker for click-to-rename, plus free-text editing and a Save button. For after-the-fact fixes, the Recordings dialog's "Name speakers" button opens each anonymous speaker's actual quotes, highlighted in the transcript, so you assign names to voices you can read, not to labels you have to remember.

Set `speaker_labels = false` under `[transcriber]` in the config to restore the old behavior (mixed mono recording, no labels). Recordings with a single source (mic-only or system-only) stay mono and unlabeled either way.

## Project structure

```
aloe-scribe/
├── config/
│   └── config.toml.example     # settings template (the app seeds your real
│                               # config from it outside the repo)
├── src/
│   ├── main.py                 # entry point and orchestration
│   ├── ui.py                   # Linux GTK and AppIndicator3 UI
│   ├── ui_mac.py               # macOS PySide6 UI
│   ├── ui_windows.py           # Windows UI (reuses ui_mac, Qt system tray)
│   ├── native_tray.py          # macOS NSStatusItem (menu-bar leaf icon)
│   ├── recorder.py             # Linux audio (PulseAudio)
│   ├── recorder_mac.py         # macOS recorder driver (spawns audio helper)
│   ├── recorder_windows.py     # Windows audio (WASAPI loopback + mic)
│   ├── transcriber.py          # whisper.cpp wrapper
│   ├── transcriber_parakeet.py # parakeet-mlx wrapper (macOS default, streaming)
│   ├── transcriber_faster_whisper.py # faster-whisper wrapper (Windows default)
│   ├── speakers.py             # diarization, tag-constrained clustering, labels
│   ├── voice_profiles.py       # per-person voice fingerprints (local only)
│   ├── summarizer.py           # local summary model (MLX, macOS)
│   ├── audio_meter.py          # audio level bar reader
│   ├── frontmatter.py          # transcript YAML header
│   ├── meeting.py              # tiny dataclass for the recording label
│   ├── syncer.py               # rclone SharePoint sync
│   └── notifications.py        # NSUserNotification and notify-send
├── tools/
│   └── aloe-audio-capture/
│       └── main.swift          # ScreenCaptureKit + AVAudioEngine helper
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
│   ├── release-mac.sh          # sign, audit, notarize, publish (maintainers)
│   ├── create-signing-cert.sh  # one-time stable signing identity (dev builds)
│   ├── fetch-model.sh          # download the macOS model from the GitHub Release
│   ├── fetch-model-windows.ps1 # download the Windows model from the GitHub Release
│   ├── publish-model.sh        # maintainers: publish the macOS model release
│   ├── publish-model-windows.sh # maintainers: publish the Windows model release
│   ├── transcribe_wav.py       # recover orphan WAVs after a crash
│   ├── health-check.sh         # Linux audio and transcription test
│   └── health-check-mac.sh     # macOS audio and transcription test
├── installer/
│   └── aloe-scribe.iss         # Inno Setup script for AloeScribeSetup.exe
├── site/                       # aloescribe.ai (GitHub Pages, auto-deploys)
├── tests/
│   ├── sample.wav              # short clip used by Windows CI
│   └── win_smoke.py            # Windows transcription smoke test
├── .github/workflows/
│   ├── windows-ci.yml          # builds and audits the Windows port
│   ├── security-audit.yml      # weekly pip-audit on both platforms
│   └── pages.yml               # deploys site/ to aloescribe.ai
├── assets/
│   └── icon.png                # aloe leaf icon
├── setup.py                    # py2app config for the macOS .app bundle
├── aloe-scribe-windows.spec    # PyInstaller config for the Windows .exe
├── RELEASING.md                # release process and the privacy rules
├── SECURITY.md                 # reporting and the network surface
├── requirements.txt            # Linux Python dependencies
├── requirements-mac.txt        # macOS Python dependencies (hash-pinned)
└── requirements-windows.txt    # Windows Python dependencies
```

## Rebuilding after a code change (macOS)

The Python source is frozen into the bundle at build time, so a code change needs a rebuild. Relaunching alone will not pick it up.

```bash
bash scripts/build-app.sh
```

To also pull the latest from GitHub in one step, use `scripts/update-mac.sh`. See [Updating](#updating-to-the-latest-version-macos).

That recompiles `bin/aloe-audio-capture`, re-runs py2app, signs everything with the stable identity, and installs the result to `/Applications`. Official releases go through `scripts/release-mac.sh`, which adds Developer ID signing, a privacy audit, and notarization. See [RELEASING.md](RELEASING.md).

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
