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

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("aloe-scribe")

# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config" / "config.toml"
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from calendar_watcher import CalendarWatcher, Meeting
from transcriber import Transcriber
from syncer import Syncer

if sys.platform == "darwin":
    from ui_mac import AloeScribeApp
    from recorder_mac import Recorder
else:
    from ui import AloeScribeApp
    from recorder import Recorder


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
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        config = load_config()
        app = AloeScribe(config)
        app.run()
    except KeyboardInterrupt:
        log.info("Interrupted — bye")
