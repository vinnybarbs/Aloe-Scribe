"""
main.py — Aloe Scribe entry point.

Wires together the calendar watcher, recorder, transcriber, syncer, and tray icon.
Run directly: python main.py
Or install as a systemd service (see scripts/install.sh)
"""

import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

# Ensure Homebrew paths are available when running from a .app bundle
# (macOS strips PATH to just /usr/bin:/bin for bundled apps)
for _p in ["/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/whisper.cpp/build/bin")]:
    if _p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _p + ":" + os.environ.get("PATH", "")

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
        logging.FileHandler(str(LOG_FILE)),
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

from calendar_watcher import CalendarWatcher, Meeting
from transcriber import Transcriber
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

        # Resolve output dir
        self.output_dir = Path(config["output"]["local_dir"]).expanduser()
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Initialise components
        self.recorder = Recorder(
            mic_source=config["audio"].get("mic_source", ""),
            system_source=config["audio"].get("system_source", ""),
        )

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
            current_mic=config["audio"].get("mic_source", ""),
            current_system=config["audio"].get("system_source", ""),
        )

        ical_url = config["calendar"]["ical_url"]
        if not ical_url:
            log.warning(
                "No iCal URL set in config.toml — calendar notifications disabled.\n"
                "Set [calendar] ical_url to enable automatic meeting detection."
            )
            self.watcher = None
        else:
            self.watcher = CalendarWatcher(
                ical_url=ical_url,
                notify_minutes=config["calendar"]["notify_minutes_before"],
                poll_interval_minutes=config["calendar"]["poll_interval_minutes"],
                on_upcoming=self._on_meeting_upcoming,
                on_ended=self._on_meeting_ended,
            )

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    def run(self):
        log.info("Aloe Scribe starting...")
        self.syncer.start()
        if self.watcher:
            self.watcher.start()
        log.info("Running — look for the Aloe Scribe icon in your system tray")
        self.tray.run()  # blocks until quit

    def _quit(self):
        log.info("Shutting down...")
        if self.recorder.is_recording():
            self.recorder.stop()
        if self.watcher:
            self.watcher.stop()
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

    # ------------------------------------------------------------------ #
    # Calendar callbacks                                                   #
    # ------------------------------------------------------------------ #

    def _on_meeting_upcoming(self, meeting: Meeting):
        """Show the Start/Skip prompt in the tray."""
        self.tray.notify_upcoming(meeting)

    def _on_meeting_ended(self, meeting: Meeting):
        """Auto-stop if still recording when the meeting ends."""
        if self.recorder.is_recording():
            log.info(f"Meeting ended — auto-stopping recording: {meeting.title}")
            self._stop_and_transcribe()

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
            log.info(f"Recording started: {self._recording_path.name}")
        else:
            log.error("Failed to start recording")
            self.tray.set_idle()

    def _stop_and_transcribe(self):
        """Stop the recorder, transcribe, sync — all on a background thread."""
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
