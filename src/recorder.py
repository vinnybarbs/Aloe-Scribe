"""
recorder.py — Captures mic + system audio simultaneously on Linux via PulseAudio/PipeWire.

Uses ffmpeg under the hood. Saves a temporary WAV file for whisper to process.
Auto-detects audio sources if not set in config.
"""

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

FFMPEG_BIN = "ffmpeg"


def _find_monitor_source() -> Optional[str]:
    """
    Auto-detect the PulseAudio monitor source for system audio capture.
    Returns the source name or None.
    """
    try:
        out = subprocess.check_output(
            ["pactl", "list", "short", "sources"], text=True
        )
        for line in out.splitlines():
            if ".monitor" in line:
                # Return the first monitor source found
                return line.split()[1]
    except Exception as e:
        log.warning(f"Could not auto-detect monitor source: {e}")
    return None


def _find_default_mic() -> str:
    """
    Find the best microphone source via PulseAudio/PipeWire.
    Prefers external USB/Bluetooth mics over built-in.
    Re-detected each time recording starts.
    """
    try:
        out = subprocess.check_output(
            ["pactl", "list", "short", "sources"], text=True
        )
        sources = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                name = parts[1]
                # Skip monitors (they capture output, not input)
                if ".monitor" in name:
                    continue
                sources.append(name)

        # Prefer USB/external mics over built-in
        for src in sources:
            if "usb" in src.lower() or "bluetooth" in src.lower():
                log.info(f"Using external mic: {src}")
                return src

        # Fall back to any non-monitor source
        if sources:
            log.info(f"Using mic: {sources[0]}")
            return sources[0]
    except Exception as e:
        log.warning(f"Could not detect mic: {e}")

    # Last resort: system default
    try:
        out = subprocess.check_output(
            ["pactl", "get-default-source"], text=True
        ).strip()
        if out:
            return out
    except Exception:
        pass
    return "default"


class Recorder:
    """
    Records mic + system audio into a single WAV file using ffmpeg.

    On PulseAudio/PipeWire, we capture:
      - Mic via the default input source
      - System audio via the .monitor source of the default sink

    Both are mixed into one stereo WAV suitable for Whisper.
    """

    def __init__(self, mic_source: str = "", system_source: str = ""):
        self._mic_config = mic_source
        self._sys_config = system_source
        self._process: Optional[subprocess.Popen] = None
        self._output_path: Optional[Path] = None

    def start(self, output_path: Path) -> bool:
        """Start recording to output_path. Returns True if started successfully."""
        if self._process:
            log.warning("Already recording")
            return False

        # Re-detect audio devices each time (handles USB mics plugged in after app start)
        self.mic_source = self._mic_config or _find_default_mic()
        self.system_source = self._sys_config or _find_monitor_source()
        log.info(f"Audio sources — mic: {self.mic_source}, system: {self.system_source or 'none'}")

        self._output_path = output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = self._build_ffmpeg_cmd(output_path)
        log.info(f"Starting recorder: {' '.join(cmd)}")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except FileNotFoundError:
            log.error("ffmpeg not found — install with: sudo apt install ffmpeg")
            return False
        except Exception as e:
            log.error(f"Failed to start recorder: {e}")
            return False

    def stop(self) -> Optional[Path]:
        """Stop recording. Returns path to the recorded WAV file."""
        if not self._process:
            log.warning("Not recording")
            return None

        log.info("Stopping recorder...")
        try:
            # Send 'q' to ffmpeg stdin for a clean stop
            self._process.stdin.write(b"q")
            self._process.stdin.flush()
            self._process.wait(timeout=10)
        except Exception:
            self._process.kill()

        self._process = None
        log.info(f"Recording saved: {self._output_path}")
        return self._output_path

    def is_recording(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _build_ffmpeg_cmd(self, output_path: Path) -> list[str]:
        """
        Build the ffmpeg command.

        If system audio monitor is available, mix mic + system audio.
        If not (e.g. Wayland without PipeWire configured), fall back to mic only.
        """
        if self.system_source:
            # Two inputs: mic + system monitor, mixed together
            cmd = [
                FFMPEG_BIN, "-y",
                "-f", "pulse", "-i", self.mic_source,       # input 0: mic
                "-f", "pulse", "-i", self.system_source,    # input 1: system
                "-filter_complex", "amix=inputs=2:duration=longest",
                "-ar", "16000",   # Whisper wants 16kHz
                "-ac", "1",       # mono
                "-c:a", "pcm_s16le",
                str(output_path),
            ]
        else:
            # Mic only fallback
            log.warning("No system audio monitor found — recording mic only")
            cmd = [
                FFMPEG_BIN, "-y",
                "-f", "pulse", "-i", self.mic_source,
                "-ar", "16000",
                "-ac", "1",
                "-c:a", "pcm_s16le",
                str(output_path),
            ]
        return cmd
