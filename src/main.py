"""
main.py — Aloe Scribe entry point.

Wires together the recorder, transcriber, syncer, and tray icon.
Run directly: python main.py
Or install as a systemd service (see scripts/install.sh)
"""

import logging
import os
import subprocess
import sys
import threading
import time
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
    if hasattr(sys, "_MEIPASS"):
        # PyInstaller (Windows): bundled data lives under _MEIPASS.
        ROOT = Path(sys._MEIPASS)
    else:
        # py2app .app bundle (macOS).
        ROOT = Path(sys.executable).parent.parent / "Resources"
else:
    ROOT = Path(__file__).parent.parent

def _resolve_config_path() -> Path:
    """User settings live OUTSIDE the app bundle. Writing config into the
    bundle broke twice at once when the signed DMG shipped: the mounted
    image is read-only, so every setting silently failed to save, and
    editing a notarized bundle's contents invalidates its signature seal.
    Frozen builds use ~/Library/Application Support, seeded once from the
    bundled config (or its example). Dev runs keep the repo config."""
    bundled = ROOT / "config" / "config.toml"
    if not getattr(sys, "frozen", False):
        return bundled
    if sys.platform == "win32":
        appsupport = (
            Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
            / "Aloe Scribe"
        )
    else:
        appsupport = (
            Path.home() / "Library" / "Application Support" / "Aloe Scribe"
        )
    cfg = appsupport / "config.toml"
    if not cfg.exists() or cfg.stat().st_size == 0:
        appsupport.mkdir(parents=True, exist_ok=True)
        example = ROOT / "config" / "config.toml.example"
        seed = example if example.exists() else bundled
        try:
            # Frozen builds default to ASCII encoding, and the template
            # holds UTF-8 bytes — always name the encoding explicitly.
            cfg.write_text(seed.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception as e:
            # Never leave a broken settings file behind — run read-only off
            # the bundled template instead.
            print(f"Config seed failed ({e}); using bundled template", file=sys.stderr)
            return seed
    return cfg


CONFIG_PATH = _resolve_config_path()
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
# Model path resolution (Windows installer ships the model next to the .exe)
# ---------------------------------------------------------------------------
def _resolve_local_model(name: str) -> str:
    """Resolve a faster-whisper model reference to a local folder.

    An absolute/existing path is used as-is. Otherwise the basename is looked up
    under models/ next to the executable (where the installer drops it), under
    ROOT, and under the current dir (from-source). Falls back to the name
    unchanged so a plain model id still works."""
    if not name:
        return name
    p = Path(name).expanduser()
    if p.is_dir():
        return str(p)
    leaf = p.name
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "models" / leaf)
    candidates.append(ROOT / "models" / leaf)
    candidates.append(Path.cwd() / "models" / leaf)
    for c in candidates:
        try:
            if c.is_dir():
                return str(c)
        except Exception:
            continue
    return name


# ---------------------------------------------------------------------------
# WAV stream helpers (split recordings are stereo: ch0 = mic, ch1 = system)
# ---------------------------------------------------------------------------
def _wav_header_channels(path: Path) -> int:
    """Channel count straight from the RIFF header. Works on a growing file
    whose data-size field is still zero (the helper patches it at stop)."""
    try:
        with open(path, "rb") as f:
            h = f.read(24)
        if len(h) >= 24 and h[:4] == b"RIFF":
            return int.from_bytes(h[22:24], "little") or 1
    except Exception:
        pass
    return 1


def _pcm_to_mono_float(raw: bytes, channels: int):
    """Interleaved int16 PCM → mono float32 in [-1, 1]. Multi-channel input is
    downmixed with a saturating sum, matching the loudness of the old
    pre-split mixed recordings."""
    import numpy as np

    samples = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        usable = (len(samples) // channels) * channels
        frames = samples[:usable].reshape(-1, channels)
        samples = np.clip(
            frames.astype(np.int32).sum(axis=1), -32768, 32767
        ).astype(np.int16)
    return samples.astype(np.float32) / 32768.0


def _recent_wav_rms(wav_path: Path, seconds: float = 30.0):
    """RMS level (0..1) of the last `seconds` of a growing 16 kHz int16 WAV.
    For split (stereo) recordings, returns the LOUDER channel's RMS — one live
    side is enough to count as activity. None when unreadable/empty, so a
    transient read error is never mistaken for silence."""
    import numpy as np

    try:
        if not wav_path.exists():
            return None
        channels = _wav_header_channels(wav_path)
        frame_bytes = 2 * channels
        size = wav_path.stat().st_size
        if size <= 44 + frame_bytes:
            return None
        want = int(seconds * 16000) * frame_bytes
        start = max(44, size - want)
        start = 44 + ((start - 44) // frame_bytes) * frame_bytes
        with open(wav_path, "rb") as f:
            f.seek(start)
            raw = f.read()
        raw = raw[: len(raw) - (len(raw) % frame_bytes)]
        if not raw:
            return None
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
        if channels > 1:
            a = a.reshape(-1, channels)
            return float(np.max(np.sqrt(np.mean(a * a, axis=0))))
        return float(np.sqrt(np.mean(a * a)))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Load config
# ---------------------------------------------------------------------------
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        log.error(f"Config not found: {CONFIG_PATH}")
        sys.exit(1)
    example = ROOT / "config" / "config.toml.example"
    base: dict = {}
    if example.exists():
        with open(example, "rb") as f:
            base = tomllib.load(f)
    with open(CONFIG_PATH, "rb") as f:
        user = tomllib.load(f)
    # Overlay per section: user values win, template fills every gap.
    for section, values in user.items():
        if isinstance(values, dict) and isinstance(base.get(section), dict):
            base[section].update(values)
        else:
            base[section] = values
    return base


# ---------------------------------------------------------------------------
# AloeScribe — main coordinator
# ---------------------------------------------------------------------------
class AloeScribe:
    def __init__(self, config: dict):
        self.config = config
        self._recording_path: Path | None = None
        self._current_meeting: Meeting | None = None
        self._watchdog_stop: Optional[threading.Event] = None
        self._max_duration_seconds = int(
            config["app"].get("max_duration_minutes", 120) * 60
        )
        # Auto-stop after this much continuous silence (0 disables). Guards
        # against forgotten recordings that run for hours after a meeting ends.
        self._silence_timeout_seconds = int(
            config["app"].get("silence_timeout_minutes", 15) * 60
        )

        # Resolve output dir
        _dir = (config["output"].get("local_dir") or "").strip()
        # None until the user picks a destination — recording is gated on it.
        self.output_dir = Path(_dir).expanduser() if _dir else None
        if self.output_dir is not None:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        # Keep the WAV next to the transcript instead of deleting it after a
        # successful transcription (~200 MB/hour). Lets a mislabeled meeting
        # be re-analyzed or re-transcribed later.
        self._keep_wav = bool(config["output"].get("keep_wav", False))

        # Speaker attribution: record mic/system as separate channels and
        # label transcript lines (M* = mic side, R* = remote side).
        self._speaker_labels = bool(
            config.get("transcriber", {}).get("speaker_labels", True)
        )
        self._diarizer = None  # built lazily on first labeled finalize
        # Transcription jobs currently running — the UI's quit path asks
        # before killing them with the interpreter.
        self._jobs_inflight = 0
        self._diarize_threshold = float(
            config.get("transcriber", {}).get("diarize_threshold", 0) or 0
        ) or None
        # Live meeting metadata from the notes window: speaker tags
        # [(elapsed_s, name)] and the notes log text. Snapshotted into the
        # stop job and crash-persisted next to the streaming draft.
        self._meeting_tags: list = []
        self._meeting_notes_text: str = ""
        self._meeting_attendees: list = []
        self._meeting_title: str = ""

        # Local executive summary after the transcript lands (small on-device
        # model via mlx-lm, subprocess-isolated).
        try:
            import speakers as _spk

            _spk.OBSIDIAN_LINKS = bool(
                config.get("output", {}).get("obsidian_links", False)
            )
        except Exception:
            pass
        scfg = config.get("summarizer", {})
        self._summarizer_enabled = bool(scfg.get("enabled", True))
        self._summarizer_model = scfg.get("model", "")

        # Initialise components
        self.recorder = Recorder(
            mic_source=config["audio"].get("mic_source", ""),
            system_source=config["audio"].get("system_source", ""),
            split_channels=self._speaker_labels,
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
            model_id = _resolve_local_model(
                tcfg.get("faster_whisper_model", FasterWhisperTranscriber.DEFAULT_MODEL)
            )
            # A local model folder (the Windows install/installer ships the
            # weights from GitHub, not Hugging Face) keeps transcription fully
            # offline.
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
            on_name_speakers=lambda p: self._maybe_prompt_speaker_names(
                Path(p), interactive=True
            ),
            processing_check=lambda: self._jobs_inflight > 0,
            on_meta_changed=self._on_meeting_meta_changed,
            on_save_transcript=self._on_save_transcript_edits,
            on_merge_transcripts=self._merge_transcript_files,
            on_resummarize=self._resummarize_existing,
            summarizer_enabled=self._summarizer_enabled,
            summarizer_available=(sys.platform == "darwin"),
            on_summarizer_toggle=self._on_summarizer_toggle,
            on_self_update=(
                self._self_update if sys.platform == "darwin" else None
            ),
            live_preview=(backend in ("parakeet", "faster_whisper")),
            current_mic=config["audio"].get("mic_source", ""),
            current_system=config["audio"].get("system_source", ""),
            current_output_dir=str(self.output_dir) if self.output_dir else "",
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
        if self._watchdog_stop is not None:
            self._watchdog_stop.set()
            self._watchdog_stop = None
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
        config_text = CONFIG_PATH.read_text(encoding="utf-8")
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
        CONFIG_PATH.write_text(config_text, encoding="utf-8")

    def _self_update(self):
        """The in-app Update button: hand off to update-mac.sh in a detached
        shell, quit so the rebuild never races the running app (replacing
        the bundle under a live process corrupts its lazy imports), and the
        script's finish reopens the new build. Returns an error message for
        the UI, or None when the handoff started."""
        script = Path.home() / "aloe-scribe" / "scripts" / "update-mac.sh"
        if not script.exists():
            return (
                "Updater not found. This install did not come from the "
                "standard setup, update from the aloe-scribe folder instead."
            )
        if self._jobs_inflight > 0:
            return "A transcript is still processing. Try again in a minute."
        log.info("Self-update starting — quitting for the updater")
        subprocess.Popen(
            [
                "/bin/bash", "-c",
                f"sleep 3; /bin/bash '{script}' > /tmp/aloe-update.log 2>&1; "
                "open -a 'Aloe Scribe'",
            ],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._quit()
        return None

    def _on_summarizer_toggle(self, enabled: bool):
        """UI checkbox for the local summary block. Applies immediately and
        persists to config.toml, section-aware since `enabled` could appear
        under other section headers someday."""
        self._summarizer_enabled = bool(enabled)
        log.info(f"Local summaries {'enabled' if enabled else 'disabled'}")
        import re

        try:
            text = CONFIG_PATH.read_text(encoding="utf-8")
            val = "true" if enabled else "false"
            m = re.search(r"(?ms)^\[summarizer\]\n(.*?)(?=^\[|\Z)", text)
            if m and re.search(r"(?m)^enabled\s*=", m.group(1)):
                section = re.sub(
                    r"(?m)^(enabled\s*=\s*)\S+", rf"\g<1>{val}",
                    m.group(1), count=1,
                )
                text = text[: m.start(1)] + section + text[m.end(1):]
            elif m:
                text = (
                    text[: m.start(1)] + f"enabled = {val}\n"
                    + m.group(1) + text[m.end(1):]
                )
            else:
                text = text.rstrip() + f"\n\n[summarizer]\nenabled = {val}\n"
            CONFIG_PATH.write_text(text, encoding="utf-8")
        except Exception as e:
            log.warning(f"Could not persist summarizer setting: {e}")

    def _on_output_dir_change(self, new_dir: str):
        """Called by UI when the user picks a new transcript destination."""
        path = Path(new_dir).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        self.output_dir = path
        log.info(f"Output directory updated → {self.output_dir}")

        # Persist to config.toml. We store the user's literal value so a path
        # under their home keeps its ~/ prefix if that's what they typed.
        import re
        config_text = CONFIG_PATH.read_text(encoding="utf-8")
        # Escape for regex replacement value
        escaped = new_dir.replace("\\", "\\\\").replace('"', '\\"')
        config_text = re.sub(
            r'local_dir\s*=\s*"[^"]*"',
            f'local_dir = "{escaped}"',
            config_text,
        )
        CONFIG_PATH.write_text(config_text, encoding="utf-8")

    # ------------------------------------------------------------------ #
    # Recording + transcription                                            #
    # ------------------------------------------------------------------ #

    def _start_recording(self, meeting: Meeting):
        """Start capturing audio for this meeting."""
        if self.output_dir is None:
            log.warning("Recording refused: no save folder chosen yet")
            return
        self._current_meeting = meeting
        self._recording_start = datetime.now()
        # Fresh meeting, fresh tags/notes (the notes window resets its own UI).
        self._meeting_tags = []
        self._meeting_notes_text = ""
        self._meeting_attendees = []
        self._chunker = None
        timestamp = self._recording_start.strftime("%Y-%m-%d-%H%M")
        slug = meeting.slug()
        self._recording_path = self.output_dir / f"{timestamp}-{slug}.wav"
        self._md_path = self.output_dir / f"{timestamp}-{slug}.md"
        # Streaming checkpoints go to a LOCAL drafts dir, not the (possibly
        # cloud-synced) output dir — repeated checkpoint writes raced the
        # final overwrite under OneDrive sync and produced conflict copies.
        self._draft_path = (
            Path.home() / ".cache" / "aloe-scribe" / "drafts"
            / f"{timestamp}-{slug}.md"
        )
        try:
            self._draft_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            self._draft_path = self._md_path  # fall back to the old behavior

        ok = self.recorder.start(self._recording_path)
        if ok:
            cap_min = self._max_duration_seconds // 60
            silence_min = self._silence_timeout_seconds // 60
            silence_note = (
                f", or after {silence_min} min of silence" if silence_min else ""
            )
            log.info(
                f"Recording started: {self._recording_path.name} "
                f"(auto-stop at {cap_min} min{silence_note})"
            )
            self._watchdog_stop = threading.Event()
            threading.Thread(
                target=self._watchdog_loop,
                args=(self._watchdog_stop,),
                daemon=True,
            ).start()
            self._start_streaming(self._recording_path, self._draft_path)
        else:
            log.error("Failed to start recording")
            self.tray.set_idle()

    # Watchdog: the safety net for forgotten recordings. A polling thread on
    # WALL-CLOCK time, not threading.Timer — macOS pauses the monotonic clock
    # during system sleep, so a one-shot 120-min timer silently stretches past
    # an entire afternoon when the lid closes or the Mac naps mid-recording
    # (that is exactly how a 2-hour cap once let a recording run 3+ hours).
    WATCHDOG_INTERVAL_S = 30.0
    # RMS below this (0..1 full scale) counts a 30 s window as silent. Speech
    # sits well above it; an idle office with digital-silence system audio
    # sits below it.
    SILENCE_RMS = 0.0045

    def _watchdog_loop(self, stop_event: threading.Event):
        last_active = time.time()
        while not stop_event.wait(self.WATCHDOG_INTERVAL_S):
            if not self.recorder.is_recording():
                return
            started = getattr(self, "_recording_start", None)
            if (
                started
                and (datetime.now() - started).total_seconds()
                >= self._max_duration_seconds
            ):
                self._auto_stop(
                    f"hit the {self._max_duration_seconds // 60}-min cap"
                )
                return
            if self._silence_timeout_seconds <= 0:
                continue
            rms = self._recent_audio_rms()
            if rms is None or rms >= self.SILENCE_RMS:
                last_active = time.time()
            elif time.time() - last_active >= self._silence_timeout_seconds:
                self._auto_stop(
                    f"no audio activity for "
                    f"{self._silence_timeout_seconds // 60} min"
                )
                return

    def _recent_audio_rms(self):
        """Recent capture level: Windows exposes a rolling in-memory ring;
        the Mac helper writes the WAV continuously, so read its tail."""
        if hasattr(self.recorder, "snapshot_recent"):
            try:
                import numpy as np

                s = self.recorder.snapshot_recent(30.0)
                if s is None or not len(s):
                    return None
                return float(
                    np.sqrt(np.mean(np.asarray(s, dtype=np.float64) ** 2))
                )
            except Exception:
                return None
        if self._recording_path is None:
            return None
        return _recent_wav_rms(self._recording_path)

    def _auto_stop(self, reason: str):
        """Watchdog-triggered stop — the transcript is still produced normally."""
        if not self.recorder.is_recording():
            return
        log.warning(f"Auto-stopping recording — {reason}")
        self.tray.set_processing()
        self._stop_and_transcribe()

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
        READ = 0.4           # read + VU-meter cadence: fast enough to act as a
                             # gate check (floor visibly zero when nobody talks,
                             # immediate bounce when someone does)
        FEED_EVERY = 4       # feed the ASR stream every 4th read (~1.6 s)
        MD_EVERY = 30.0      # checkpoint the partial transcript to .md every ~30 s
        elapsed = 0.0
        last_md = 0.0
        tick = 0
        pending = []         # samples read but not yet fed to the stream
        channels = 0         # resolved from the WAV header once data arrives

        # The transcriber runs all MLX work (open, feed, close) on its single
        # inference thread, so the stream stays on the thread that loaded the model.
        # The stream stays open after this loop. Stop finalizes and closes it.
        if not self.transcriber.stream_open(depth=2):
            log.warning("Streaming unavailable (model not loaded)")
            return
        log.info("Streaming transcription started")
        while not stop_event.is_set():
            if stop_event.wait(READ):
                break
            elapsed += READ
            tick += 1
            try:
                if not wav_path.exists():
                    continue
                if wav_path.stat().st_size - self._stream_offset < 3200:  # < ~0.1 s
                    continue
                if channels == 0:
                    channels = _wav_header_channels(wav_path)
                    self._stream_wav_channels = channels
                    # Process-as-you-go: chunk-transcribe the meeting WHILE
                    # it records so Stop costs seconds, not minutes. Stereo
                    # split recordings only; batch remains the fallback.
                    if (
                        channels in (1, 2)
                        and self._speaker_labels
                        and hasattr(self.transcriber, "transcribe_sentences")
                    ):
                        try:
                            import speakers

                            self._chunker = speakers.IncrementalChunker(
                                self.transcriber.transcribe_sentences,
                                tmp_dir=self._draft_path.parent,
                            )
                            log.info("Incremental transcription enabled")
                        except Exception as e:
                            log.warning(f"Incremental transcription unavailable: {e}")
                frame_bytes = 2 * channels
                with open(wav_path, "rb") as f:
                    f.seek(self._stream_offset)
                    raw = f.read()
                consumed = len(raw) - (len(raw) % frame_bytes)
                if consumed <= 0:
                    continue
                self._stream_offset += consumed
                chunker = getattr(self, "_chunker", None)
                if chunker is not None:
                    try:
                        chunker.feed(raw[:consumed], channels)
                    except Exception as e:
                        log.warning(f"Incremental feed failed: {e}")
                # VU bars on every read — the meter's job is the gate check
                # (floor at zero in silence, instant bounce on speech), so it
                # updates at READ cadence, not the slower ASR cadence. Levels
                # come from the audio actually being recorded — no second
                # capture process fighting the recorder for the device.
                try:
                    if hasattr(self.tray, "meter_levels"):
                        import numpy as np

                        a = np.frombuffer(raw[:consumed], dtype=np.int16)
                        if channels == 2:
                            a = a[: (len(a) // 2) * 2].reshape(-1, 2)
                            mic_pk = float(np.abs(a[:, 0]).max()) / 32768.0
                            sys_pk = float(np.abs(a[:, 1]).max()) / 32768.0
                        else:
                            mic_pk = float(np.abs(a).max()) / 32768.0 if len(a) else 0.0
                            sys_pk = 0.0
                        self.tray.meter_levels(mic_pk, sys_pk)
                except Exception:
                    pass
                # Split recordings are stereo; the live stream wants the mix.
                pending.append(_pcm_to_mono_float(raw[:consumed], channels))
                if tick % FEED_EVERY != 0:
                    continue
                if chunker is not None:
                    try:
                        chunker.maybe_process()
                    except Exception as e:
                        log.warning(f"Incremental step failed: {e}")
                import numpy as np

                samples = np.concatenate(pending) if len(pending) > 1 else pending[0]
                pending = []
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
        if self._watchdog_stop is not None:
            self._watchdog_stop.set()
            self._watchdog_stop = None
        # Stop the live loop and wait for it to release the inference thread.
        if getattr(self, "_live_stop", None) is not None:
            self._live_stop.set()
        if getattr(self, "_stream_thread", None) is not None:
            self._stream_thread.join(timeout=4)
            self._stream_thread = None
        # Snapshot this recording's identity NOW — the user can start the next
        # recording while this one is still transcribing, which repoints all
        # the self._* fields at the new meeting.
        job = {
            "md_path": getattr(self, "_md_path", None),
            "title": self._meeting_title
            or (
                self._current_meeting.title if self._current_meeting else "Recording"
            ),
            "custom_title": bool(self._meeting_title),
            "when": getattr(self, "_recording_start", None) or datetime.now(),
            "draft_path": getattr(self, "_draft_path", None),
            "tags": list(self._meeting_tags),
            "notes_text": self._meeting_notes_text,
            "attendees": list(self._meeting_attendees),
            "chunker": getattr(self, "_chunker", None),
        }
        self._chunker = None
        wav_path = self.recorder.stop()
        # Hand the chunker the audio the live loop had not reached yet, so
        # finalize() only has the last minute or two left to transcribe.
        if job["chunker"] is not None and wav_path:
            try:
                channels = getattr(self, "_stream_wav_channels", 2)
                frame_bytes = 2 * channels
                with open(wav_path, "rb") as f:
                    f.seek(getattr(self, "_stream_offset", 44))
                    raw = f.read()
                raw = raw[: len(raw) - (len(raw) % frame_bytes)]
                if raw:
                    job["chunker"].feed(raw, channels)
            except Exception as e:
                log.warning(f"Tail feed to chunker failed: {e}")
        # The stream is preview + crash-checkpoint only. Its sentence
        # timestamps are window-relative (parakeet-mlx's streaming decoder
        # never applies a chunk offset, unlike batch), so transcripts built
        # from it showed [00:00]-ish times for hour-long meetings — and the
        # speaker labeler windows audio by timestamp, so it needs real ones.
        # The final transcript therefore ALWAYS comes from a batch pass. The
        # streamed text is still complete, though — pin it to the processing
        # screen as a draft so the wait never looks like a hang.
        try:
            draft = ""
            if hasattr(self.transcriber, "stream_text"):
                draft = self.transcriber.stream_text()
            if draft and hasattr(self.tray, "processing_draft"):
                self.tray.processing_draft(draft)
        except Exception:
            pass
        try:
            self.transcriber.stream_close()
        except Exception:
            pass
        if not wav_path:
            self.tray.set_idle()
            return
        threading.Thread(
            target=self._transcribe_and_sync,
            args=(wav_path,),
            kwargs={"job": job},
            daemon=True,
        ).start()

    def _labeled_markdown(
        self,
        wav_path: Path,
        title: str,
        when: datetime,
        tags: list = None,
        notes_text: str = "",
        attendees: list = None,
    ) -> str:
        """Full per-channel labeled transcript markdown, or '' when labeling
        is disabled, the WAV is mono (single-source recording), the backend
        has no sentence API, or anything in the pipeline fails. `tags` are
        the live speaker tags; `notes_text` the notes-window log."""
        if not self._speaker_labels:
            return ""
        if not hasattr(self.transcriber, "transcribe_sentences"):
            return ""
        try:
            import speakers

            if self._diarizer is None:
                self._diarizer = speakers.Diarizer(
                    threshold=self._diarize_threshold
                )
            source = (
                "aloe-scribe-windows" if sys.platform == "win32" else "aloe-scribe-mac"
            )

            def progress(msg: str):
                try:
                    if hasattr(self.tray, "processing_status"):
                        self.tray.processing_status(msg)
                except Exception:
                    pass

            # Set expectations up front: N minutes of audio takes roughly
            # N/12 minutes to label (per-channel STT with concurrent
            # diarization, measured on Apple Silicon).
            try:
                channels = _wav_header_channels(wav_path)
                dur_min = wav_path.stat().st_size / (32000 * channels) / 60
                est = max(1, round(dur_min / 12))
                progress(
                    f"{round(dur_min)} min recording. Labeling usually "
                    f"takes about {est} min."
                )
            except Exception:
                pass

            return (
                speakers.build_labeled_transcript(
                    wav_path,
                    self.transcriber.transcribe_sentences,
                    self._diarizer,
                    title,
                    when,
                    source,
                    progress=progress,
                    tags=tags,
                    notes=speakers.parse_notes_log(notes_text),
                    attendees=attendees,
                )
                or ""
            )
        except Exception as e:
            log.warning(f"Speaker labeling failed — keeping unlabeled transcript: {e}")
            return ""

    def _maybe_prompt_speaker_names(self, md_path: Path, interactive: bool = False):
        """After a labeled transcript is saved, ask the user who each speaker
        was (a few quotes per label, full transcript alongside) and rewrite
        the file with the typed names. Best-effort and fully skippable.
        `interactive` marks an explicit user request (the Done screen's
        "Name speakers…" button) — failures get a visible dialog then."""
        # The automatic post-processing popup is gone (it asked users to
        # reverse-engineer voices from text, which never worked). This dialog
        # now opens ONLY on explicit request — the Done screen's button.
        if not hasattr(self.tray, "prompt_speaker_names"):
            return
        if not interactive:
            return
        try:
            import speakers

            md_path = Path(md_path)
            text = md_path.read_text(encoding="utf-8")
            quotes = speakers.speaker_quotes(text)
            if not quotes:
                if interactive:
                    self._show_error(
                        f"{md_path.name} has no speaker labels to name."
                    )
                return

            def apply(mapping: dict):
                try:
                    current = md_path.read_text(encoding="utf-8")
                    renamed = speakers.apply_speaker_names(current, mapping)
                    if renamed != current:
                        md_path.write_text(renamed, encoding="utf-8")
                        log.info(f"Speaker names applied: {md_path.name}")
                        # Re-sync so the named version replaces the labeled one.
                        self.syncer.enqueue(md_path)
                        # Names changed, so the summary's owners are stale —
                        # rebuild it with the real names.
                        self._resummarize_existing(md_path)
                except Exception as e:
                    log.error(f"Applying speaker names failed: {e}")

            self.tray.prompt_speaker_names(quotes, text, apply)
        except Exception as e:
            log.debug(f"Speaker naming prompt skipped: {e}")

    def _on_meeting_meta_changed(
        self, tags: list, notes_text: str, attendees: list = None,
        title: str = "",
    ):
        """Debounced pushes from the notes window. Held for the stop job and
        crash-persisted next to the streaming draft."""
        self._meeting_tags = list(tags or [])
        self._meeting_notes_text = notes_text or ""
        self._meeting_attendees = list(attendees or [])
        self._meeting_title = (title or "").strip()
        draft = getattr(self, "_draft_path", None)
        if draft is None:
            return
        try:
            import json

            draft.with_suffix(".meta.json").write_text(
                json.dumps(
                    {
                        "tags": self._meeting_tags,
                        "notes": self._meeting_notes_text,
                        "attendees": self._meeting_attendees,
                    }
                ),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _merge_transcript_files(self, paths: list):
        """Merge transcripts of one split meeting (chosen in the Recordings
        browser) into the earliest part's file; the other part files are
        removed, so the summary agent sees exactly one meeting."""
        try:
            import speakers

            files = [Path(p) for p in paths]
            texts = {p: p.read_text(encoding="utf-8") for p in files}
            merged = speakers.merge_transcripts(list(texts.values()))
            if not merged:
                self._show_error(
                    "Could not merge. The selected files do not look like "
                    "parts of one meeting, each needs a date header."
                )
                return
            from datetime import timezone

            far_future = datetime.max.replace(tzinfo=timezone.utc)
            target = min(
                files,
                key=lambda p: speakers.transcript_date(texts[p]) or far_future,
            )
            target.write_text(merged, encoding="utf-8")
            for p in files:
                if p != target:
                    try:
                        p.unlink()
                    except Exception:
                        pass
            log.info(f"Merged {len(files)} transcripts into {target.name}")
            self.syncer.enqueue(target)
            try:
                import notifications

                notifications.send(
                    "Aloe Scribe",
                    f"Merged {len(files)} transcripts into {target.name}",
                )
            except Exception:
                pass
        except Exception as e:
            log.error(f"Transcript merge failed: {e}")
            self._show_error(f"Merge failed: {e}")

    def _on_save_transcript_edits(self, path, text: str):
        """The notes window's Save button: write the user's edited transcript
        and re-sync it."""
        p = Path(path)
        p.write_text(text, encoding="utf-8")
        log.info(f"Transcript edits saved: {p.name}")
        self.syncer.enqueue(p)

    def _assembled_markdown(self, wav_path: Path, title: str, when, job: dict) -> str:
        """Transcript from sentences transcribed DURING the meeting (the
        process-as-you-go path). '' when there was no chunker or anything
        fails — callers fall through to the batch pipeline."""
        chunker = job.get("chunker")
        if chunker is None or not self._speaker_labels:
            return ""
        try:
            import speakers

            def progress(msg: str):
                try:
                    if hasattr(self.tray, "processing_status"):
                        self.tray.processing_status(msg)
                except Exception:
                    pass

            progress("Finishing the last minute of transcription…")
            per_channel = chunker.finalize()
            if not any(per_channel.values()):
                return ""
            if self._diarizer is None:
                self._diarizer = speakers.Diarizer(
                    threshold=self._diarize_threshold
                )
            source = (
                "aloe-scribe-windows" if sys.platform == "win32" else "aloe-scribe-mac"
            )
            md = speakers.assemble_labeled_transcript(
                wav_path,
                per_channel,
                self._diarizer,
                title,
                when,
                source,
                progress=progress,
                tags=job.get("tags"),
                notes=speakers.parse_notes_log(job.get("notes_text", "")),
                attendees=job.get("attendees"),
            )
            if md:
                log.info(
                    f"Assembled transcript from {chunker.chunks_done} live chunks"
                )
            return md or ""
        except Exception as e:
            log.warning(f"Incremental assembly failed, falling back to batch: {e}")
            return ""

    def _append_plain_extras(self, md_path: Path, title: str, when, job: dict):
        """The plain fallback writes only frontmatter and dialogue. Bring it
        up to the standard document: H1 title, the attendee roster, and the
        user's typed notes. They wrote those during the meeting, and losing
        them to a fallback path is not acceptable."""
        try:
            import speakers

            text = md_path.read_text(encoding="utf-8")
            if text.startswith("# ") or "\n# " in text[:400]:
                return  # already structured
            end = text.find("---", 3)
            if end < 0:
                return
            end = text.find("\n", end) + 1
            meta = [f"# {title}"]
            roster = [
                str(a).strip() for a in (job.get("attendees") or [])
                if str(a).strip()
            ]
            if roster:
                meta.append("")
                meta.append(f"Attendees: {speakers._fmt_names(roster)}")
            notes_md = speakers.build_notes_section(
                speakers.parse_notes_log(job.get("notes_text", ""))
            )
            body = text[end:].lstrip("\n")
            md_path.write_text(
                text[:end] + "\n" + "\n".join(meta) + notes_md
                + "\n\n## Transcript\n\n" + body,
                encoding="utf-8",
            )
        except Exception as e:
            log.warning(f"Could not append meeting extras: {e}")

    def _resummarize_existing(self, md_path):
        """Recordings browser 'Name speakers' follow-up: rebuild the summary
        of an already-finished transcript off the UI thread."""
        import threading

        threading.Thread(
            target=lambda: self._summarize_and_refresh(Path(md_path)),
            daemon=True,
        ).start()

    def _summarize_and_refresh(self, md_path: Path):
        """Add the local executive summary AFTER the transcript is safe on
        disk, so a summarizer failure can never cost content. Re-syncs and
        refreshes the notes window when it lands."""
        if not self._summarizer_enabled:
            return
        try:
            import summarizer

            model = summarizer.resolve_model(self._summarizer_model)
            if not summarizer.summarize_file(md_path, model):
                return
            self.syncer.enqueue(md_path)
            try:
                if hasattr(self.tray, "notes_show_final"):
                    self.tray.notes_show_final(
                        md_path, md_path.read_text(encoding="utf-8")
                    )
            except Exception:
                pass
            try:
                import notifications

                notifications.send(
                    "Aloe Scribe", f"Summary added: {md_path.name}"
                )
            except Exception:
                pass
        except Exception as e:
            log.warning(f"Summary step failed: {e}")

    def _mono_stt_input(self, wav_path: Path):
        """Return (stt_input, tmp_mono). For stereo split recordings stt_input
        is a hidden temp mono downmix (tmp_mono set — caller deletes it); mono
        recordings pass through as (wav_path, None)."""
        if _wav_header_channels(wav_path) != 2:
            return wav_path, None
        try:
            import speakers

            tmp = wav_path.with_name(f".{wav_path.stem}.mono.wav")
            if speakers.downmix_to_mono_wav(wav_path, tmp):
                return tmp, tmp
        except Exception as e:
            log.warning(f"Downmix failed, feeding stereo WAV to STT: {e}")
        return wav_path, None

    def _transcribe_and_sync(self, wav_path: Path, job: dict = None):
        self._jobs_inflight += 1
        try:
            self._transcribe_and_sync_inner(wav_path, job)
        finally:
            self._jobs_inflight -= 1

    def _transcribe_and_sync_inner(self, wav_path: Path, job: dict = None):
        # `job` is the identity snapshot taken at Stop. Callers without one
        # (recovery paths) fall back to the live fields.
        job = job or {}
        meeting = self._current_meeting
        title = job.get("title") or (meeting.title if meeting else "Recording")
        when = (
            job.get("when")
            or getattr(self, "_recording_start", None)
            or datetime.now()
        )
        md_path = (
            job.get("md_path")
            or getattr(self, "_md_path", None)
            # Recovery with no folder chosen: land beside the WAV itself.
            or ((self.output_dir or wav_path.parent) / f"{wav_path.stem}.md")
        )
        # A user-typed meeting title becomes the FILENAME too — retrieval
        # agents rank on it, and "manual-recording" made every meeting look
        # identical. Keep the timestamp prefix so files still sort.
        if job.get("custom_title"):
            try:
                stamp = "-".join(md_path.stem.split("-")[:4])
                slug = Meeting(title=title).slug()
                md_path = md_path.with_name(f"{stamp}-{slug}.md")
            except Exception:
                pass

        # A recording that survived a hard app kill has a valid audio body but
        # an unfinalized (zero-size) header — patch it before reading.
        try:
            import speakers

            speakers.repair_wav_header(wav_path)
        except Exception:
            pass

        # Preferred path for split recordings: sentences already transcribed
        # incrementally during the meeting — only diarization and rendering
        # remain, which is seconds. Then per-channel batch, then a plain
        # transcribe of the mono downmix. Guard against the backend raising —
        # an uncaught exception here would kill this daemon thread silently,
        # leaving the WAV orphaned with no transcript and no notice.
        result = None
        md = self._assembled_markdown(wav_path, title, when, job)
        if not md:
            md = self._labeled_markdown(
                wav_path,
                title,
                when,
                tags=job.get("tags"),
                notes_text=job.get("notes_text", ""),
                attendees=job.get("attendees"),
            )
        if md:
            try:
                md_path.parent.mkdir(parents=True, exist_ok=True)
                md_path.write_text(md, encoding="utf-8")
                result = md_path
                log.info(f"Transcript saved (speaker-labeled): {md_path}")
            except Exception as e:
                log.error(f"Writing labeled transcript failed: {e}")

        tmp_mono = None
        if result is None:
            stt_input, tmp_mono = self._mono_stt_input(wav_path)
            try:
                result = self.transcriber.transcribe(
                    audio_path=stt_input,
                    output_path=md_path,
                    meeting_title=title,
                    meeting_date=when,
                )
                if result:
                    # Fallback output still carries the user's meeting
                    # artifacts (title H1, roster, typed notes).
                    self._append_plain_extras(md_path, title, when, job)
            except Exception as e:
                log.error(f"Transcription raised: {e}")

        if tmp_mono is not None:
            try:
                tmp_mono.unlink()
            except Exception:
                pass

        # If the user already started the NEXT recording while this one was
        # transcribing, its screen owns the window — announce this transcript
        # with a notification instead of hijacking the UI state.
        recording_again = self.recorder.is_recording()

        if result:
            # The final transcript exists — the local streaming draft served
            # its crash-insurance purpose.
            draft = job.get("draft_path")
            if draft and draft != md_path:
                for stale in (draft, draft.with_suffix(".meta.json")):
                    try:
                        stale.unlink()
                    except Exception:
                        pass
            # Only delete the WAV once we actually have the transcript (and
            # not even then when keep_wav is set).
            if not self._keep_wav:
                try:
                    wav_path.unlink()
                    log.debug(f"Deleted WAV: {wav_path.name}")
                except Exception:
                    pass
            if recording_again:
                try:
                    import notifications

                    notifications.send(
                        "Aloe Scribe", f"Transcript ready: {md_path.name}"
                    )
                except Exception:
                    pass
            else:
                self.tray.set_done(md_path)
            self.syncer.enqueue(md_path)
            # Load the finished transcript into the notes window (if open)
            # for click-to-rename and free editing. No popup.
            try:
                if hasattr(self.tray, "notes_show_final"):
                    self.tray.notes_show_final(
                        md_path, md_path.read_text(encoding="utf-8")
                    )
            except Exception:
                pass
            self._summarize_and_refresh(md_path)
        else:
            # Keep the WAV so the recording can be recovered. Surface the exact
            # command to re-run it.
            log.error(
                f"Transcription failed — WAV kept for recovery: {wav_path}\n"
                f"  Recover with: python3 scripts/transcribe_wav.py {wav_path}"
            )
            if not recording_again:
                self.tray.set_idle()
            self._show_error(
                f"Transcription of {wav_path.name} failed. The audio is "
                "kept, retry it from the Recordings browser."
            )

        if not recording_again:
            self._current_meeting = None

    # ------------------------------------------------------------------ #
    # Transcribe an existing audio file chosen in the UI file picker       #
    # ------------------------------------------------------------------ #

    def _show_error(self, msg: str):
        """Surface an error in the UI when the platform tray supports it
        (Linux GTK does not); the log always gets it either way."""
        try:
            if hasattr(self.tray, "show_error"):
                self.tray.show_error(msg)
        except Exception:
            pass

    def _transcribe_existing_file(self, wav_path):
        self._jobs_inflight += 1
        try:
            self._transcribe_existing_file_inner(wav_path)
        finally:
            self._jobs_inflight -= 1

    def _transcribe_existing_file_inner(self, wav_path):
        """Transcribe an audio file the user picked in the UI.

        Mirrors the live recording path: infers the meeting title/date from the
        filename, runs the configured backend, and updates the tray. The audio
        file is kept on success — only live recordings delete their WAV.
        """
        wav_path = Path(wav_path).expanduser()
        if not wav_path.exists():
            log.error(f"WAV not found: {wav_path}")
            self.tray.set_idle()
            self._show_error(f"File not found: {wav_path}")
            return

        # A header-only WAV (~44 bytes) is a recording that captured no audio
        # (e.g. the app was killed at start). Say so plainly instead of
        # failing into the log — a silent failure reads as a frozen button.
        try:
            if wav_path.stat().st_size < 1024:
                log.error(f"File contains no audio: {wav_path}")
                self.tray.set_idle()
                self._show_error(
                    f"{wav_path.name} contains no audio. The recording it "
                    "came from captured nothing, so there is nothing to "
                    "transcribe. You can delete the file."
                )
                return
        except Exception:
            pass

        title, when = self._infer_meeting_from_filename(wav_path)
        md_path = wav_path.with_suffix(".md")
        log.info(f"Transcribing existing file: {wav_path.name} → {md_path.name}")

        result = None
        md = self._labeled_markdown(wav_path, title, when)
        if md:
            try:
                md_path.write_text(md, encoding="utf-8")
                result = md_path
                log.info(f"Transcript saved (speaker-labeled): {md_path}")
            except Exception as e:
                log.error(f"Writing labeled transcript failed: {e}")

        tmp_mono = None
        if result is None:
            stt_input, tmp_mono = self._mono_stt_input(wav_path)
            try:
                result = self.transcriber.transcribe(
                    audio_path=stt_input,
                    output_path=md_path,
                    meeting_title=title,
                    meeting_date=when,
                )
            except Exception as e:
                log.error(f"Transcription raised: {e}")

        if tmp_mono is not None:
            try:
                tmp_mono.unlink()
            except Exception:
                pass

        if result:
            self.tray.set_done(md_path)
            self.syncer.enqueue(md_path)
            try:
                if md and hasattr(self.tray, "notes_show_final"):
                    self.tray.notes_show_final(
                        md_path, md_path.read_text(encoding="utf-8")
                    )
            except Exception:
                pass
            self._summarize_and_refresh(md_path)
        else:
            log.error(f"Could not transcribe {wav_path}")
            self.tray.set_idle()
            self._show_error(
                f"Could not transcribe {wav_path.name}. The file may be "
                "corrupt or in an unsupported format. Details are in "
                "/tmp/aloe-scribe.log."
            )

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
