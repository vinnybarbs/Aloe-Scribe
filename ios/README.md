# Aloe Scribe — iOS

The iPhone counterpart to the desktop Aloe Scribe. Records audio through the
**microphone**, transcribes **on-device** with [WhisperKit](https://github.com/argmaxinc/WhisperKit),
and writes a Markdown transcript — in the same format as the desktop app — into
a folder you pick in the Files app.

## Why mic-only (and why that's fine)

iOS sandboxes every app: one app **cannot** capture the audio of another, and
there is no system-audio loopback. So unlike the Mac build (which taps system
audio via ScreenCaptureKit), the iPhone build captures **acoustically through
the mic**:

- **In person** — the mic hears everyone in the room. Works perfectly.
- **A call on speaker (on another device)** — put the call on speakerphone on a
  *second* device; this phone's mic hears the other side. Works.

### Honest limitation

During an **active native phone/FaceTime call on _this_ device**, iOS gives the
microphone to the call and blocks third-party capture. So you can't transcribe
your own live call from the same phone — only in-person audio, or a call playing
out loud on a separate device. The in-app **"switch to speaker"** banner covers
the cases that *do* work.

## How it works

1. Tap **Choose…** and pick a folder (an iCloud Drive folder syncs everywhere).
2. (Optional) name the conversation and pick a Whisper model.
3. Tap **Start Recording** — records mic to a 16 kHz WAV.
4. Tap **Stop & Transcribe** — WhisperKit runs locally, then the `.md` transcript
   is written into your chosen folder. The temp WAV is deleted.

Recording auto-stops after the max-duration cap (default 120 min), just like the
desktop `max_duration_minutes`.

### Recording keeps running when the phone sleeps

The app declares the `audio` background mode (`Info.plist` → `UIBackgroundModes`),
so once a session is recording it **keeps capturing after the screen locks or you
switch apps** — the session is held active until you tap **Stop**. Transient
interruptions (Siri, an alarm, a notification chime) are handled too:
`AudioRecorder` listens for `AVAudioSession.interruptionNotification` and resumes
into the same file when the interruption ends. The auto-stop cap is measured
against wall-clock time, so it still fires correctly even while backgrounded.

(One thing iOS still won't allow: an incoming/active native phone call seizes the
mic — see the limitation above.)

## Build

This is a [XcodeGen](https://github.com/yonaskolb/XcodeGen) project (no
`.xcodeproj` is checked in — it's generated).

```bash
brew install xcodegen
cd ios
xcodegen generate
open AloeScribe.xcodeproj
```

Then in Xcode set your signing team and run on a device (the Simulator has no
real mic and WhisperKit wants the Neural Engine, so use a physical iPhone).

First transcription downloads the selected Whisper model and caches it on the
device; after that it runs fully offline.

### Requirements

- iOS 17+, an Apple-Silicon-class iPhone (A14/A15 or newer recommended)
- Xcode 15+

## How it maps to the desktop app

| Desktop (`src/`) | iOS (`ios/AloeScribe/`) |
|---|---|
| `recorder_mac.py` (mic) | `Audio/AudioRecorder.swift` |
| ScreenCaptureKit system audio | *dropped — not possible / not needed on iOS* |
| `transcriber*.py` | `Transcription/TranscriptionService.swift` (WhisperKit) |
| `config.toml` | `Models/Settings.swift` (UserDefaults) |
| "Save transcripts to" folder | `Storage/BookmarkStore.swift` (security-scoped bookmark) |
| Markdown output / filename format | `Storage/TranscriptWriter.swift` (identical format) |
| YAML frontmatter (`src/frontmatter.py`) | `TranscriptWriter.frontmatter(...)` (identical block) |

### Transcript frontmatter

Every transcript starts with a YAML block carrying the meeting's **time-window
identity** — nothing about transcription changes, it's just metadata at the top
of the `.md`:

```yaml
---
title: "Weekly Sync"
date: 2026-06-17T14:00:00-05:00
end:  2026-06-17T14:42:00-05:00
duration_min: 42
source: aloe-scribe-ios
---
```

This is the handoff to whatever work AI you point at the file (Copilot, Glean,
etc.): the `date`/`end` window lets that agent match the calendar event and
infer attendees + related email/chat itself. No participants are listed here on
purpose. The desktop writers emit the identical block via `src/frontmatter.py`.
| `main.py` orchestration | `Models/RecordingSession.swift` |
| `ui_mac.py` | `Views/ContentView.swift` |

## Layout

```
ios/
├── project.yml                         # XcodeGen project definition
└── AloeScribe/
    ├── App/AloeScribeApp.swift          # @main entry point
    ├── Models/
    │   ├── Settings.swift               # persisted settings (config.toml analog)
    │   └── RecordingSession.swift       # record → transcribe → save orchestration
    ├── Audio/AudioRecorder.swift        # mic → 16 kHz WAV
    ├── Transcription/TranscriptionService.swift  # WhisperKit wrapper
    ├── Storage/
    │   ├── BookmarkStore.swift          # user-picked folder bookmark
    │   └── TranscriptWriter.swift       # Markdown render + write
    ├── Views/
    │   ├── ContentView.swift            # main screen
    │   └── FolderPicker.swift           # UIDocumentPicker wrapper
    └── Resources/Info.plist             # mic usage string, background audio
```
