"""
recorder_mac.py — Captures mic + system audio on macOS via avfoundation.

Uses ffmpeg with the avfoundation backend. System audio capture requires
BlackHole (brew install --cask blackhole-2ch) and a Multi-Output Device
configured in Audio MIDI Setup.
"""

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

FFMPEG_BIN = "ffmpeg"


def _list_avfoundation_devices() -> dict:
    """
    Parse ffmpeg -f avfoundation -list_devices to find audio devices.
    Returns {"audio": [(index, name), ...]}
    """
    try:
        result = subprocess.run(
            [FFMPEG_BIN, "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=10,
        )
        # ffmpeg prints device list to stderr
        output = result.stderr
    except Exception as e:
        log.warning(f"Could not list avfoundation devices: {e}")
        return {"audio": []}

    audio_devices = []
    in_audio = False
    for line in output.splitlines():
        if "AVFoundation audio devices:" in line:
            in_audio = True
            continue
        if in_audio:
            # Lines look like: [AVFoundation ...] [0] MacBook Pro Microphone
            m = re.search(r"\[(\d+)\]\s+(.*)", line)
            if m:
                audio_devices.append((int(m.group(1)), m.group(2).strip()))
            elif "AVFoundation" not in line:
                break

    return {"audio": audio_devices}


def _find_default_mic() -> Optional[str]:
    """Find the built-in or external microphone (not BlackHole)."""
    devices = _list_avfoundation_devices()
    for idx, name in devices.get("audio", []):
        lower = name.lower()
        if "blackhole" in lower:
            continue  # skip virtual devices
        if "microphone" in lower or "mic" in lower or "input" in lower:
            log.info(f"Using mic: [{idx}] {name}")
            return f":{idx}"
    # Fallback: first non-BlackHole audio device
    for idx, name in devices.get("audio", []):
        if "blackhole" not in name.lower():
            log.info(f"Using audio device as mic: [{idx}] {name}")
            return f":{idx}"
    log.warning("No microphone found")
    return ":0"


def _find_blackhole() -> Optional[str]:
    """
    Look for a BlackHole device in avfoundation audio devices.
    Returns the device string (e.g. \":1\") or None.
    """
    devices = _list_avfoundation_devices()
    for idx, name in devices.get("audio", []):
        if "blackhole" in name.lower():
            log.info(f"Found BlackHole audio device: [{idx}] {name}")
            return f":{idx}"
    return None


class Recorder:
    """
    Records mic + system audio into a single WAV file using ffmpeg on macOS.

    Mic is captured via avfoundation default audio input.
    System audio is captured via BlackHole virtual audio device (if installed).
    """

    def __init__(self, mic_source: str = "", system_source: str = ""):
        self.mic_source = mic_source or _find_default_mic()
        self.system_source = system_source or _find_blackhole()
        self._process: Optional[subprocess.Popen] = None
        self._output_path: Optional[Path] = None

        if self.system_source:
            log.info(f"Audio sources — mic: {self.mic_source}, system: {self.system_source}")
        else:
            log.warning(
                "BlackHole not detected — system audio won't be captured.\n"
                "Install with: brew install --cask blackhole-2ch\n"
                "Then create a Multi-Output Device in Audio MIDI Setup."
            )

    def start(self, output_path: Path) -> bool:
        """Start recording to output_path. Returns True if started successfully."""
        if self._process:
            log.warning("Already recording")
            return False

        self._output_path = output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)

        cmd = self._build_ffmpeg_cmd(output_path)
        log.info(f"Starting recorder: {' '.join(cmd)}")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            # Give ffmpeg a moment to fail (e.g. permission denied)
            import time
            time.sleep(0.5)
            if self._process.poll() is not None:
                _, stderr = self._process.communicate()
                log.error(f"ffmpeg exited immediately: {stderr.decode(errors='replace')}")
                if "Permission" in stderr.decode(errors='replace') or "denied" in stderr.decode(errors='replace'):
                    log.error(
                        "Microphone access denied. Grant permission in:\n"
                        "  System Settings → Privacy & Security → Microphone"
                    )
                self._process = None
                return False
            return True
        except FileNotFoundError:
            log.error("ffmpeg not found — install with: brew install ffmpeg")
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
        """Build the ffmpeg command for macOS avfoundation capture."""
        if self.system_source:
            # Two inputs: mic + BlackHole system audio, mixed together
            cmd = [
                FFMPEG_BIN, "-y",
                "-f", "avfoundation", "-i", self.mic_source,
                "-f", "avfoundation", "-i", self.system_source,
                "-filter_complex", "amix=inputs=2:duration=longest",
                "-ar", "16000",
                "-ac", "1",
                "-c:a", "pcm_s16le",
                str(output_path),
            ]
        else:
            # Mic only fallback
            log.warning("No system audio device — recording mic only")
            cmd = [
                FFMPEG_BIN, "-y",
                "-f", "avfoundation", "-i", self.mic_source,
                "-ar", "16000",
                "-ac", "1",
                "-c:a", "pcm_s16le",
                str(output_path),
            ]
        return cmd
