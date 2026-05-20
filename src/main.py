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
            log.info(f"Transcriber backend: parakeet ({model_id})")
            self.transcriber = ParakeetTranscriber(model=model_id)
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
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        slug = meeting.slug()
        wav_name = f"{timestamp}-{slug}.wav"
        self._recording_path = self.output_dir / wav_name

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

    def _stop_and_transcribe(self):
        """Stop the recorder, transcribe, sync — all on a background thread."""
        self._cancel_max_duration_timer()
        wav_path = self.recorder.stop()
        if not wav_path:
            self.tray.set_idle()
            return

        # Run transcription in background so the tray stays responsive
        thread = threading.Thread(
            target=self._transcribe_and_sync,
            args=(wav_path,),
            daemon=True,
        )
        thread.start()

    def _transcribe_and_sync(self, wav_path: Path):
        meeting = self._current_meeting
        now = datetime.now()

        # Build output markdown path
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H%M")
        slug = meeting.slug() if meeting else "recording"
        md_filename = f"{date_str}-{time_str}-{slug}.md"
        md_path = self.output_dir / md_filename

        # Transcribe
        result = self.transcriber.transcribe(
            audio_path=wav_path,
            output_path=md_path,
            meeting_title=meeting.title if meeting else "Recording",
            meeting_date=now,
        )

        # Clean up the WAV (already have the transcript)
        try:
            wav_path.unlink()
            log.debug(f"Deleted WAV: {wav_path.name}")
        except Exception:
            pass

        if result:
            self.tray.set_done(md_path)
            self.syncer.enqueue(md_path)
        else:
            log.error("Transcription failed")
            self.tray.set_idle()

        self._current_meeting = None


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
