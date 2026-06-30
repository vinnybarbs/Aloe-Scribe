"""
main.py — Aloe Scribe entry point.

Wires together the recorder, transcriber, syncer, and tray icon.
Run directly: python main.py
Or install as a systemd service (see scripts/install.sh)
"""

import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

# Ensure Homebrew paths are available when running from a .app bundle
# (macOS strips PATH to just /usr/bin:/bin for bundled apps)
for _p in ["/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/whisper.cpp/build/bin")]:
    if _p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _p + ":" + os.environ.get("PATH", "")

# When running from a py2app frozen bundle, mlx + parakeet_mlx + huggingface_hub
# aren't bundled (mlx's namespace-package + native-lib layout breaks py2app's
# modulegraph). Pull them from the project venv at runtime by prepending its
# site-packages to sys.path. We locate the venv by:
#   1. $ALOE_SCRIBE_VENV   (explicit override — recommended for CI/dev)
#   2. Same-named .venv beside the bundle's parent project directory
#   3. ~/aloe-scribe/.venv  (default install location from scripts/install-mac.sh)
if getattr(sys, "frozen", False):
    from pathlib import Path as _Path
    _candidates = []
    if os.environ.get("ALOE_SCRIBE_VENV"):
        _candidates.append(_Path(os.environ["ALOE_SCRIBE_VENV"]))
    _candidates.append(_Path.home() / "aloe-scribe" / ".venv")
    for _venv in _candidates:
        _site = _venv / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        if _site.is_dir():
            if str(_site) not in sys.path:
                sys.path.insert(0, str(_site))
            break

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

# ---------------------------------------------------------------------------
# Logging — write to /tmp/aloe-scribe.log so we can debug bundled app
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    # /tmp does not exist on Windows; use the per-user temp dir.
    LOG_FILE = Path(os.environ.get("TEMP", os.environ.get("TMP", "."))) / "aloe-scribe.log"
else:
    LOG_FILE = Path("/tmp/aloe-scribe.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        # encoding="utf-8" because py2app's bundled Python defaults to ASCII
        # for FileHandler, which trips on em-dashes etc. in log messages.
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
    ],
)
log = logging.getLogger("aloe-scribe")

# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    # Running inside a py2app .app bundle
    ROOT = Path(sys.executable).parent.parent / "Resources"
else:
    ROOT = Path(__file__).parent.parent

CONFIG_PATH = ROOT / "config" / "config.toml"
SRC = ROOT / "src"
if SRC.exists():
    sys.path.insert(0, str(SRC))

from meeting import Meeting
from transcriber import Transcriber
from transcriber_parakeet import ParakeetTranscriber
from syncer import Syncer

if sys.platform == "darwin":
    from ui_mac import AloeScribeApp
    from recorder_mac import Recorder, list_sources
elif sys.platform == "win32":
    from ui_windows import AloeScribeApp
    from recorder_windows import Recorder, list_sources
else:
    from ui import AloeScribeApp
    from recorder import Recorder, list_sources


# ---------------------------------------------------------------------------
# Load config
# ---------------------------------------------------------------------------
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.error(f"Config not found: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# AloeScribe — main coordinator
# ---------------------------------------------------------------------------
class AloeScribe:
    def __init__(self, config: dict):
        self.config = config
        self._recording_path: Path | None = None
        self._current_meeting: Meeting | None = None
        self._max_duration_timer: Optional[threading.Timer] = None
        self._max_duration_seconds = int(
            config["app"].get("max_duration_minutes", 120) * 60
        )

        # Resolve output dir
        self.output_dir = Path(config["output"]["local_dir"]).expanduser()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Initialise components
        self.recorder = Recorder(
            mic_source=config["audio"].get("mic_source", ""),
            system_source=config["audio"].get("system_source", ""),
        )

        backend = config.get("transcriber", {}).get("backend", "whisper").lower()
        if backend == "parakeet":
            model_id = config.get("transcriber", {}).get(
                "parakeet_model", ParakeetTranscriber.DEFAULT_MODEL
            )
            # When the model is a local directory (the default install fetches
            # the weights from GitHub, not Hugging Face), force HF offline mode.
            # That way a blocked/absent Hugging Face never causes a hang or a
            # surprise network call — the weights load straight from disk.
            if Path(model_id).expanduser().is_dir():
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            log.info(f"Transcriber backend: parakeet ({model_id})")
            self.transcriber = ParakeetTranscriber(model=model_id)
        elif backend == "faster_whisper":
            from transcriber_faster_whisper import FasterWhisperTranscriber

            tcfg = config.get("transcriber", {})
            model_id = tcfg.get(
                "faster_whisper_model", FasterWhisperTranscriber.DEFAULT_MODEL
            )
            # A local model folder (the Windows install fetches weights from
            # GitHub, not Hugging Face) keeps transcription fully offline.
            if Path(model_id).expanduser().is_dir():
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            log.info(f"Transcriber backend: faster-whisper ({model_id})")
            self.transcriber = FasterWhisperTranscriber(
                model=model_id,
                device=tcfg.get("faster_whisper_device", "auto"),
                language=tcfg.get("language", "en"),
            )
        else:
            log.info("Transcriber backend: whisper.cpp")
            self.transcriber = Transcriber(
                binary_path=config["whisper"]["binary_path"],
                model_path=config["whisper"]["model_path"],
            )

        self.syncer = Syncer(
            rclone_remote=config["sync"]["rclone_remote"],
            enabled=config["sync"]["enabled"],
            notify_on_sync=config["app"]["notify_on_sync"],
        )

        self.tray = AloeScribeApp(
            on_start_recording=self._start_recording,
            on_stop_recording=self._stop_and_transcribe,
            on_quit=self._quit,
            list_sources=list_sources,
            on_device_change=self._on_device_change,
            on_output_dir_change=self._on_output_dir_change,
            on_transcribe_file=self._transcribe_existing_file,
            live_preview=(backend in ("parakeet", "faster_whisper")),
            current_mic=config["audio"].get("mic_source", ""),
            current_system=config["audio"].get("system_source", ""),
            current_output_dir=str(self.output_dir),
        )

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def run(self):
        log.info("Aloe Scribe starting...")
        self.syncer.start()
        log.info("Running — look for the Aloe Scribe icon in your system tray")
        self.tray.run()  # blocks until quit

    def _quit(self):
        log.info("Shutting down...")
        self._cancel_max_duration_timer()
        if self.recorder.is_recording():
            wav_path = self.recorder.stop()
            if wav_path:
                log.warning(
                    f"Recording was in progress at shutdown — WAV saved but NOT transcribed. "
                    f"Recover with: python3 scripts/transcribe_wav.py {wav_path}"
                )
        self.syncer.stop()

    # ------------------------------------------------------------------ #
    # Device selection                                                     #
    # ------------------------------------------------------------------ #

    def _on_device_change(self, mic_source: str, system_source: str):
        """Called by UI when user selects a different audio device."""
        self.recorder._mic_config = mic_source
        self.recorder._sys_config = system_source
        log.info(f"Device selection updated — mic: {mic_source or '(auto)'}, system: {system_source or '(auto)'}")

        # Persist to config.toml
        import re
        config_text = CONFIG_PATH.read_text()
        config_text = re.sub(
            r'mic_source\s*=\s*"[^"]*"',
            f'mic_source = "{mic_source}"',
            config_text,
        )
        config_text = re.sub(
            r'system_source\s*=\s*"[^"]*"',
            f'system_source = "{system_source}"',
            config_text,
        )
        CONFIG_PATH.write_text(config_text)

    def _on_output_dir_change(self, new_dir: str):
        """Called by UI when the user picks a new transcript destination."""
        path = Path(new_dir).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        self.output_dir = path
        log.info(f"Output directory updated → {self.output_dir}")

        # Persist to config.toml. We store the user's literal value so a path
        # under their home keeps its ~/ prefix if that's what they typed.
        import re
        config_text = CONFIG_PATH.read_text()
        # Escape for regex replacement value
        escaped = new_dir.replace("\\", "\\\\").replace('"', '\\"')
        config_text = re.sub(
            r'local_dir\s*=\s*"[^"]*"',
            f'local_dir = "{escaped}"',
            config_text,
        )
        CONFIG_PATH.write_text(config_text)

    # ------------------------------------------------------------------ #
    # Recording + transcription                                            #
    # ------------------------------------------------------------------ #

    def _start_recording(self, meeting: Meeting):
        """Start capturing audio for this meeting."""
        self._current_meeting = meeting
        self._recording_start = datetime.now()
        timestamp = self._recording_start.strftime("%Y-%m-%d-%H%M")
        slug = meeting.slug()
        self._recording_path = self.output_dir / f"{timestamp}-{slug}.wav"
        # Transcript shares the WAV's timestamp so live streaming can write to it.
        self._md_path = self.output_dir / f"{timestamp}-{slug}.md"

        ok = self.recorder.start(self._recording_path)
        if ok:
            cap_min = self._max_duration_seconds // 60
            log.info(
                f"Recording started: {self._recording_path.name} (auto-stop in {cap_min} min)"
            )
            self._max_duration_timer = threading.Timer(
                self._max_duration_seconds, self._auto_stop
            )
            self._max_duration_timer.daemon = True
            self._max_duration_timer.start()
            self._start_streaming(self._recording_path, self._md_path)
        else:
            log.error("Failed to start recording")
            self.tray.set_idle()

    def _auto_stop(self):
        """Fired by the duration timer — safety net so a forgotten recording doesn't run forever."""
        if not self.recorder.is_recording():
            return
        log.warning(
            f"Recording hit the {self._max_duration_seconds // 60}-min cap — auto-stopping"
        )
        self.tray.set_processing()
        self._stop_and_transcribe()

    def _cancel_max_duration_timer(self):
        if self._max_duration_timer:
            self._max_duration_timer.cancel()
            self._max_duration_timer = None

    # ------------------------------------------------------------------ #
    # Live streaming transcription (real-time, the whole recording)        #
    # ------------------------------------------------------------------ #

    def _start_streaming(self, wav_path: Path, md_path: Path):
        """Stream-transcribe the recording in real time: feed new audio to a
        cache-aware Parakeet stream every ~1.5 s, push the growing transcript to
        the UI, and checkpoint it to the .md every ~30 s for crash durability.
        Parakeet-only and isolated — a failure here never affects the recording
        or the clean final transcript written at Stop.
        """
        # Windows (faster-whisper) has no true streaming API, so it gets a live
        # preview instead: re-transcribe a rolling window of recent audio. The
        # final transcript still comes from the full pass at Stop. Handled before
        # the Parakeet guard; the Mac path below is unchanged.
        from transcriber_faster_whisper import FasterWhisperTranscriber

        if isinstance(self.transcriber, FasterWhisperTranscriber):
            self._start_fw_preview()
            return

        if not isinstance(self.transcriber, ParakeetTranscriber):
            return
        self._live_stop = threading.Event()
        self._stream_offset = 44  # WAV header; shared so finalize can catch up
        meeting = self._current_meeting
        title = meeting.title if meeting else "Recording"
        start_dt = getattr(self, "_recording_start", datetime.now())
        try:
            self.tray.live_preview_clear()
            self.tray.live_preview_status(
                "STT model is starting and the transcript will show within 30 seconds."
            )
        except Exception:
            pass
        self._stream_thread = threading.Thread(
            target=self._streaming_loop,
            args=(wav_path, md_path, title, start_dt, self._live_stop),
            daemon=True,
        )
        self._stream_thread.start()

    def _streaming_loop(self, wav_path, md_path, title, start_dt, stop_event):
        import numpy as np

        STEP = 1.5           # feed new audio every ~1.5 s
        MD_EVERY = 30.0      # checkpoint the partial transcript to .md every ~30 s
        elapsed = 0.0
        last_md = 0.0

        # The transcriber runs all MLX work (open, feed, close) on its single
        # inference thread, so the stream stays on the thread that loaded the model.
        # The stream stays open after this loop. Stop finalizes and closes it.
        if not self.transcriber.stream_open(depth=2):
            log.warning("Streaming unavailable (model not loaded)")
            return
        log.info("Streaming transcription started")
        while not stop_event.is_set():
            if stop_event.wait(STEP):
                break
            elapsed += STEP
            try:
                if not wav_path.exists():
                    continue
                if wav_path.stat().st_size - self._stream_offset < 3200:  # < ~0.1 s
                    continue
                with open(wav_path, "rb") as f:
                    f.seek(self._stream_offset)
                    raw = f.read()
                consumed = len(raw) - (len(raw) % 2)
                if consumed <= 0:
                    continue
                self._stream_offset += consumed
                samples = (
                    np.frombuffer(raw[:consumed], dtype=np.int16).astype(np.float32)
                    / 32768.0
                )
                text = self.transcriber.stream_feed(samples)
                if text:
                    self.tray.live_preview_set(text)
                    if elapsed - last_md >= MD_EVERY:
                        md = self.transcriber.stream_markdown(title, start_dt)
                        if md:
                            try:
                                md_path.parent.mkdir(parents=True, exist_ok=True)
                                md_path.write_text(md, encoding="utf-8")
                                last_md = elapsed
                            except Exception as e:
                                log.debug(f"Streaming .md checkpoint failed: {e}")
            except Exception as e:
                log.info(f"Streaming step error: {e}")
        log.info("Streaming transcription ended")

    def _start_fw_preview(self):
        """Live preview for the faster-whisper backend (Windows). Re-transcribes
        a rolling window of recent audio and pushes it to the preview box. The
        final transcript still comes from the full pass at Stop, so this is a
        liveness indicator, not the saved output. Reuses _live_stop / _stream_thread
        so the existing Stop handling tears it down."""
        if not hasattr(self.recorder, "snapshot_recent"):
            return
        self._live_stop = threading.Event()
        try:
            self.tray.live_preview_clear()
            self.tray.live_preview_status(
                "STT model is starting and a live sample will show within 30 seconds."
            )
        except Exception:
            pass
        self._stream_thread = threading.Thread(
            target=self._fw_preview_loop, args=(self._live_stop,), daemon=True
        )
        self._stream_thread.start()

    def _fw_preview_loop(self, stop_event):
        PREVIEW_EVERY = 8.0   # re-transcribe the recent window about this often
        WINDOW_SEC = 20.0     # show the last ~20 s of speech
        # Warm the model so the first sample appears promptly.
        try:
            self.transcriber.preload()
        except Exception:
            pass
        log.info("Live preview started (faster-whisper rolling window)")
        while not stop_event.is_set():
            if stop_event.wait(PREVIEW_EVERY):
                break
            try:
                samples = self.recorder.snapshot_recent(WINDOW_SEC)
                if samples is None or len(samples) < 16000:
                    continue  # < ~1 s captured so far (16 kHz mono)
                text = self.transcriber.transcribe_samples(samples)
                if text:
                    self.tray.live_preview_set(text)
            except Exception as e:
                log.info(f"Preview step error: {e}")
        log.info("Live preview ended")

    def _stop_and_transcribe(self):
        """Stop the recorder and finalize the transcript on a background thread."""
        self._cancel_max_duration_timer()
        # Stop the live loop and wait for it to release the inference thread.
        if getattr(self, "_live_stop", None) is not None:
            self._live_stop.set()
        if getattr(self, "_stream_thread", None) is not None:
            self._stream_thread.join(timeout=4)
            self._stream_thread = None
        wav_path = self.recorder.stop()
        if not wav_path:
            try:
                self.transcriber.stream_close()
            except Exception:
                pass
            self.tray.set_idle()
            return

        # Parakeet: the stream already holds the transcript, so finalize from it
        # (fast). Whisper: there is no stream, so transcribe the file.
        target = (
            self._finalize_transcript
            if isinstance(self.transcriber, ParakeetTranscriber)
            else self._transcribe_and_sync
        )
        threading.Thread(target=target, args=(wav_path,), daemon=True).start()

    def _finalize_transcript(self, wav_path: Path):
        """Save the streamed transcript. Feed the last bit of audio the live loop
        did not reach, write the .md from the stream result, and we are done. No
        full re-transcribe. Falls back to a batch transcribe only if the stream
        produced nothing."""
        import numpy as np

        meeting = self._current_meeting
        title = meeting.title if meeting else "Recording"
        when = getattr(self, "_recording_start", None) or datetime.now()
        md_path = getattr(self, "_md_path", None) or (
            self.output_dir / f"{wav_path.stem}.md"
        )

        streamed_text = ""
        md = ""
        try:
            offset = getattr(self, "_stream_offset", 44)
            if wav_path.exists():
                with open(wav_path, "rb") as f:
                    f.seek(offset)
                    raw = f.read()
                consumed = len(raw) - (len(raw) % 2)
                if consumed > 0:
                    samples = (
                        np.frombuffer(raw[:consumed], dtype=np.int16).astype(np.float32)
                        / 32768.0
                    )
                    self.transcriber.stream_feed(samples)
            streamed_text = self.transcriber.stream_text()
            md = self.transcriber.stream_markdown(title, when)
        except Exception as e:
            log.error(f"Stream finalize error: {e}")
        finally:
            try:
                self.transcriber.stream_close()
            except Exception:
                pass

        if streamed_text and md:
            try:
                md_path.parent.mkdir(parents=True, exist_ok=True)
                md_path.write_text(md, encoding="utf-8")
                try:
                    wav_path.unlink()
                except Exception:
                    pass
                self.tray.set_done(md_path)
                self.syncer.enqueue(md_path)
                self._current_meeting = None
                return
            except Exception as e:
                log.error(f"Writing streamed transcript failed: {e}")

        log.info("Stream produced no transcript. Falling back to a full transcribe.")
        self._transcribe_and_sync(wav_path)

    def _transcribe_and_sync(self, wav_path: Path):
        meeting = self._current_meeting
        # Use the path + start time fixed at record start (the live stream may
        # already have checkpointed a partial transcript here; this clean pass
        # overwrites it).
        when = getattr(self, "_recording_start", None) or datetime.now()
        md_path = getattr(self, "_md_path", None) or (
            self.output_dir / f"{wav_path.stem}.md"
        )

        # Transcribe. Guard against the backend raising (e.g. a Parakeet Metal
        # error) — an uncaught exception here would kill this daemon thread
        # silently, leaving the WAV orphaned with no transcript and no notice.
        result = None
        try:
            result = self.transcriber.transcribe(
                audio_path=wav_path,
                output_path=md_path,
                meeting_title=meeting.title if meeting else "Recording",
                meeting_date=when,
            )
        except Exception as e:
            log.error(f"Transcription raised: {e}")

        if result:
            # Only delete the WAV once we actually have the transcript.
            try:
                wav_path.unlink()
                log.debug(f"Deleted WAV: {wav_path.name}")
            except Exception:
                pass
            self.tray.set_done(md_path)
            self.syncer.enqueue(md_path)
        else:
            # Keep the WAV so the recording can be recovered. Surface the exact
            # command to re-run it.
            log.error(
                f"Transcription failed — WAV kept for recovery: {wav_path}\n"
                f"  Recover with: python3 scripts/transcribe_wav.py {wav_path}"
            )
            self.tray.set_idle()

        self._current_meeting = None

    # ------------------------------------------------------------------ #
    # Transcribe an existing WAV chosen from the UI dropdown               #
    # ------------------------------------------------------------------ #

    def _transcribe_existing_file(self, wav_path):
        """Transcribe an already-recorded WAV the user picked in the UI.

        Mirrors the live recording path: infers the meeting title/date from the
        filename, runs the configured backend, and updates the tray. The WAV is
        kept on success here — the dropdown lists only WAVs without a .md
        sibling, so a successful run drops it from the list naturally without
        deleting the audio.
        """
        wav_path = Path(wav_path).expanduser()
        if not wav_path.exists():
            log.error(f"WAV not found: {wav_path}")
            self.tray.set_idle()
            return

        title, when = self._infer_meeting_from_filename(wav_path)
        md_path = wav_path.with_suffix(".md")
        log.info(f"Transcribing existing file: {wav_path.name} → {md_path.name}")

        result = None
        try:
            result = self.transcriber.transcribe(
                audio_path=wav_path,
                output_path=md_path,
                meeting_title=title,
                meeting_date=when,
            )
        except Exception as e:
            log.error(f"Transcription raised: {e}")

        if result:
            self.tray.set_done(md_path)
            self.syncer.enqueue(md_path)
        else:
            log.error(f"Could not transcribe {wav_path}")
            self.tray.set_idle()

    @staticmethod
    def _infer_meeting_from_filename(wav_path: Path):
        """Parse YYYY-MM-DD-HHMM-slug.wav → (title, datetime). Falls back to mtime."""
        import re

        m = re.match(r"^(\d{4}-\d{2}-\d{2})-(\d{4})-(.+)$", wav_path.stem)
        if m:
            date_str, time_str, slug = m.groups()
            try:
                when = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H%M")
                return slug.replace("-", " ").title(), when
            except ValueError:
                pass
        return (
            wav_path.stem.replace("-", " ").title(),
            datetime.fromtimestamp(wav_path.stat().st_mtime),
        )


# ---------------------------------------------------------------------------
# Audio device setup (--setup flag)
# ---------------------------------------------------------------------------
def run_setup():
    """Interactive audio device picker — writes choice to config.toml."""
    if sys.platform != "darwin":
        print("Audio setup is for macOS. On Linux, devices auto-detect via PulseAudio.")
        return

    from recorder_mac import _list_avfoundation_devices, _is_virtual

    devices = _list_avfoundation_devices()
    audio = devices.get("audio", [])

    if not audio:
        print("No audio devices found.")
        return

    print("\n  Available audio devices:\n")
    for idx, name in audio:
        tag = ""
        if _is_virtual(name):
            tag = " (virtual — skipped)"
        elif "blackhole" in name.lower():
            tag = " (system audio capture)"
        print(f"    [{idx}] {name}{tag}")

    print()
    mic = input("  Enter mic device number (or press Enter for auto-detect): ").strip()
    sys_audio = input("  Enter system audio device number (or Enter to skip): ").strip()

    # Read current config
    config_path = CONFIG_PATH
    config_text = config_path.read_text()

    if mic:
        # Save by NAME so it works even when device indices change
        # (e.g. AirPods connecting/disconnecting shuffles indices)
        chosen_name = next((n for i, n in audio if str(i) == mic), "")
        if chosen_name:
            import re
            config_text = re.sub(
                r'mic_source\s*=\s*"[^"]*"',
                f'mic_source = "{chosen_name}"',
                config_text,
            )
            print(f"  Mic set to: {chosen_name}")
            print(f"  (Saved by name — will auto-find the right device each launch)")

    if sys_audio:
        chosen_name = next((n for i, n in audio if str(i) == sys_audio), "")
        if chosen_name:
            import re
            config_text = re.sub(
                r'system_source\s*=\s*"[^"]*"',
                f'system_source = "{chosen_name}"',
                config_text,
            )
            print(f"  System audio set to: {chosen_name}")

    config_path.write_text(config_text)
    print(f"\n  Config saved to {config_path}")
    print(f"  Leave mic_source blank in config to always auto-detect.\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if "--setup" in sys.argv:
        run_setup()
    else:
        try:
            config = load_config()
            app = AloeScribe(config)
            app.run()
        except KeyboardInterrupt:
            log.info("Interrupted — bye")
