# Windows test and build runbook

Everything here runs on the Windows PC. The repo is ready as of August 31,
2026: the Windows UI passes the full callback set, the PyInstaller spec
carries the lazily imported modules, and the summarizer explicitly skips
itself on Windows. Budget one to two hours for the first full pass.

## What Windows gets today

Capture (mic + system audio via WASAPI loopback, no virtual cable),
streaming transcription with faster-whisper, speaker diarization via
sherpa-onnx with M/R labels, live tagging, the notepad UI, the recordings
browser with merge and retro naming, and the agent-first document layout.

Known gaps, by design for now: no summary block (the summarizer needs
Apple's mlx), no echo cancellation (use a headset for clean tests), no
tag-constrained clustering or voice profiles (those ride the Mac-only Senko
path), and flat VU meters.

## 1. Prerequisites (10 min)

1. Windows 10 or 11, admin account.
2. Install Git: https://git-scm.com/download/win (defaults are fine).
3. Install Python 3.12 x64 from https://python.org (CHECK "Add python.exe
   to PATH" during install). Not the Microsoft Store build, not ARM64.

## 2. Install and run from source (15 min)

Open PowerShell:

```
git clone https://github.com/vinnybarbs/Aloe-Scribe.git
cd Aloe-Scribe
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
powershell -ExecutionPolicy Bypass -File scripts\run-windows.ps1
```

The installer creates the venv, installs dependencies, and fetches the
whisper model. If the app window appears, the port runs.

## 3. Test matrix (30 min)

Work through these in order, noting anything that fails:

1. Devices: the mic dropdown lists the real microphones. Pick one.
2. Record 2 minutes while a YouTube video plays through the speakers.
   Stop. The transcript must contain the video's words (system audio
   capture) under R labels and your voice under M labels.
3. Live streaming: transcript text appears in the strip DURING recording.
4. Tagging: add two attendees to the roster, click one while speaking.
   The final transcript should carry the name.
5. Title: set a meeting title; the saved file name and H1 must use it.
6. Notes: type notes during recording; they must appear in the document.
7. Recordings browser: Open Folder works (Explorer), Name speakers on the
   finished transcript opens the quotes dialog, renames apply.
8. Auto-stop: not practical to wait 2 hours, skip.
9. Quit during processing: the guard dialog must appear, not a dead app.
10. A real Teams or Zoom call with a headset, the meeting-shaped test.

## 4. Build the standalone app (20 min plus iteration)

```
powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
```

Output lands in `dist\Aloe Scribe\`. Launch `dist\Aloe Scribe\Aloe
Scribe.exe` and repeat tests 1 to 3. If the exe dies on launch with a
missing module or data file, add it to `hiddenimports` or `datas` in
`aloe-scribe-windows.spec` and rebuild; that iteration is expected on the
first build.

Then the installer:

```
powershell -ExecutionPolicy Bypass -File scripts\build-installer-windows.ps1
```

## 5. Publish

Upload the installer artifact to a GitHub release on vinnybarbs/Aloe-Scribe
(for example tag `windows-v0.1`). A PC user then installs with one download
and never touches Python. Commit any spec fixes made during step 4 and push
them from the PC (or send them back for committing).

## Troubleshooting

- `install-windows.ps1` fails on a package: note which one, rerun. CUDA is
  optional; CPU transcription works everywhere, just slower.
- No system audio in the transcript: Windows Sound settings, make sure the
  playback device the video used is the default output (loopback follows
  the default output device).
- Mic silent: Windows Settings, Privacy and security, Microphone, allow
  desktop apps.
- Transcription far behind real time: expected on old CPUs; note the CPU
  model, this calibrates the minimum spec.
