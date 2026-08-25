"""
recorder_mac.py — macOS audio capture for Aloe Scribe.

Uses the Swift helper binary `aloe-audio-capture` to tap system audio via
ScreenCaptureKit (no BlackHole / Multi-Output Device setup required) and mix
in the selected mic via an ffmpeg child process. With both sources active the
helper writes a 16 kHz stereo WAV (ch0 = mic, ch1 = system) so speakers can
be attributed downstream; single-source recordings are mono.

The avfoundation device enumeration is kept here because the macOS UI still
populates the mic dropdown from it.
"""

import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

FFMPEG_BIN = "ffmpeg"


# ---------------------------------------------------------------------------
# Helper-binary discovery
# ---------------------------------------------------------------------------

def _helper_path() -> Path:
    """
    Locate the aloe-audio-capture Swift helper binary.

    - When running from a py2app .app bundle: lives in Contents/Resources/bin
    - When running from a source checkout: lives in <repo>/bin
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent.parent / "Resources" / "bin" / "aloe-audio-capture"
    return Path(__file__).resolve().parent.parent / "bin" / "aloe-audio-capture"


# ---------------------------------------------------------------------------
# avfoundation device enumeration (UI dropdown only — capture goes via SCK)
# ---------------------------------------------------------------------------

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
            m = re.search(r"\[(\d+)\]\s+(.*)", line)
            if m:
                audio_devices.append((int(m.group(1)), m.group(2).strip()))
            elif "AVFoundation" not in line:
                break

    return {"audio": audio_devices}


# Virtual/software audio devices to skip when auto-detecting a mic.
# Also filters Continuity phone/tablet mics: merely opening one (the idle
# meter's fallback did) makes the phone chime in the user's pocket, and a
# phone mic is never the right choice for a meeting recorder.
_VIRTUAL_DEVICES = {
    "blackhole", "microsoft teams", "zoomaudio", "zoom audio",
    "iphone", "ipad",
}


def _is_virtual(name: str) -> bool:
    lower = name.lower()
    return any(v in lower for v in _VIRTUAL_DEVICES)


# ---------------------------------------------------------------------------
# avfoundation-index helpers (used by ui_mac's VU meters, not by capture)
# ---------------------------------------------------------------------------

def _resolve_device(config_value: str) -> Optional[str]:
    """
    Map a stored device value to an avfoundation index string like ":4".
    Accepts:
      - "" (empty) → None, caller falls back to auto-detect
      - ":N" → returned as-is
      - "<name>" → looked up by case-insensitive substring match
    """
    if not config_value:
        return None
    if config_value.startswith(":"):
        return config_value
    devices = _list_avfoundation_devices()
    lower = config_value.lower()
    for idx, name in devices.get("audio", []):
        if lower in name.lower():
            return f":{idx}"
    return None


def _find_default_mic() -> str:
    """avfoundation-index variant of mic auto-detect. Returns ':N' or ':0'."""
    devices = _list_avfoundation_devices()
    physical = [(idx, name) for idx, name in devices.get("audio", [])
                if not _is_virtual(name)]
    if not physical:
        return ":0"
    for idx, name in physical:
        lower = name.lower()
        if "macbook" not in lower and "built" not in lower:
            return f":{idx}"
    return f":{physical[0][0]}"


def _find_blackhole() -> Optional[str]:
    """Locate BlackHole's avfoundation index (used only by the legacy meter)."""
    devices = _list_avfoundation_devices()
    for idx, name in devices.get("audio", []):
        if "blackhole" in name.lower():
            return f":{idx}"
    return None


def _find_default_mic_name() -> str:
    """
    Return the *name* of the best physical mic (refreshed each call). Skips
    virtual devices and prefers external mics over the built-in. Empty string
    if no input is available.
    """
    devices = _list_avfoundation_devices()
    physical = [(idx, name) for idx, name in devices.get("audio", [])
                if not _is_virtual(name)]
    if not physical:
        log.warning("No physical microphone found")
        return ""
    # Prefer external mics (USB / wired) over built-in.
    for _idx, name in physical:
        lower = name.lower()
        if "macbook" not in lower and "built" not in lower:
            log.info(f"Auto-detected mic: {name}")
            return name
    name = physical[0][1]
    log.info(f"Auto-detected mic: {name}")
    return name


def list_sources() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """
    List available audio sources on macOS for the UI dropdowns.
    Returns (mic_sources, system_sources) as lists of (id, display_name).

    Mic list is the real set of physical inputs from avfoundation (id == name
    so we get a stable identifier across reboots / device-index shuffles).

    System list is a single synthetic option — ScreenCaptureKit always
    captures the desktop's mix, regardless of which BlackHole / Multi-Output
    Device is configured.
    """
    devices = _list_avfoundation_devices()
    mics = []
    for _idx, name in devices.get("audio", []):
        if _is_virtual(name):
            continue
        mics.append((name, name))
    system = [("system", "System Audio (built-in)")]
    return mics, system


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

class Recorder:
    """
    Records mic + system audio into a single 16 kHz mono WAV via the Swift
    helper. System audio is captured by ScreenCaptureKit; mic is captured by
    a child ffmpeg process the helper spawns. The two streams are
    sample-aligned and mixed inside the helper before being written.

    `mic_source`: avfoundation device name (e.g. "Jabra SPEAK 510 USB"), or
        empty/auto for auto-detect.
    `system_source`: kept for UI/config compatibility. Treated as a boolean:
        falsy ("", "off", "none") means do not capture system audio. Anything
        else (including the synthetic "system" id) enables SCK capture.
    """

    def __init__(self, mic_source: str = "", system_source: str = "",
                 split_channels: bool = True):
        self._mic_config = mic_source
        self._sys_config = system_source
        self._split_config = split_channels
        self._process: Optional[subprocess.Popen] = None
        self._output_path: Optional[Path] = None
        # True while a --split recording is running: the WAV is stereo
        # (ch0 = mic, ch1 = system) rather than mixed mono.
        self.split_channels = False

    def _resolved_mic_name(self) -> str:
        if self._mic_config:
            return self._mic_config
        return _find_default_mic_name()

    def _capture_system(self) -> bool:
        # Default = capture system audio (SCK makes it always available).
        # Only explicit off keywords disable it. Treat legacy BlackHole names
        # as "on" too so a pre-SCK config keeps working.
        v = (self._sys_config or "").strip().lower()
        return v not in {"off", "none", "false", "0", "no", "disabled"}

    def start(self, output_path: Path) -> bool:
        """Start recording to output_path. Returns True if started successfully."""
        if self._process:
            log.warning("Already recording")
            return False

        helper = _helper_path()
        if not helper.exists() or not os.access(helper, os.X_OK):
            log.error(
                f"Audio helper not found or not executable: {helper}\n"
                "Rebuild it with: swiftc -O tools/aloe-audio-capture/main.swift -o bin/aloe-audio-capture"
            )
            return False

        mic_name = self._resolved_mic_name()
        capture_system = self._capture_system()
        if not mic_name and not capture_system:
            log.error("Refusing to record: no mic and no system audio")
            return False

        self._output_path = output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)

        cmd: list[str] = [str(helper), "--output", str(output_path)]
        if mic_name:
            cmd += ["--mic", mic_name]
        if not capture_system:
            cmd += ["--no-system"]
        # Split needs both sources; single-source recordings stay mono.
        self.split_channels = bool(
            self._split_config and mic_name and capture_system
        )
        if self.split_channels:
            cmd += ["--split"]

        log.info(f"Starting recorder: {' '.join(cmd)}")
        # Helper stderr goes to a dedicated log file so we can see what SCK +
        # mixer are doing without polluting (or being lost in) the main app
        # log's Python message stream.
        helper_log = Path("/tmp/aloe-helper.log")
        try:
            self._helper_stderr = open(helper_log, "ab")
            self._helper_stderr.write(
                f"\n========== {datetime.now().isoformat(timespec='seconds')} : {' '.join(cmd)} ==========\n".encode()
            )
            self._helper_stderr.flush()
        except Exception as e:
            log.warning(f"Could not open helper log {helper_log}: {e}")
            self._helper_stderr = subprocess.DEVNULL
        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=self._helper_stderr,
            )
        except FileNotFoundError:
            log.error(f"Helper binary missing: {helper}")
            return False
        except Exception as e:
            log.error(f"Failed to spawn helper: {e}")
            return False

        # Give the helper ~0.5 s to fail (e.g. Screen Recording denied).
        time.sleep(0.5)
        if self._process.poll() is not None:
            log.error(
                "Helper exited immediately. Inspect /tmp/aloe-helper.log for the cause "
                "(common: Screen Recording or Microphone permission denied — grant in "
                "System Settings → Privacy & Security → {Screen Recording | Microphone})."
            )
            self._process = None
            return False
        return True

    def stop(self) -> Optional[Path]:
        """Stop recording. Returns the path to the recorded WAV file."""
        if not self._process:
            log.warning("Not recording")
            return None

        log.info("Stopping recorder...")
        try:
            assert self._process.stdin is not None
            self._process.stdin.write(b"q")
            self._process.stdin.flush()
            self._process.stdin.close()
            self._process.wait(timeout=15)
        except Exception as e:
            log.warning(f"Clean stop failed ({e}) — terminating")
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except Exception:
                self._process.kill()

        self._process = None
        # Close the helper-stderr log file (was opened in start()).
        try:
            if hasattr(self, "_helper_stderr") and hasattr(self._helper_stderr, "close"):
                self._helper_stderr.close()
        except Exception:
            pass
        log.info(f"Recording saved: {self._output_path}")
        return self._output_path

    def is_recording(self) -> bool:
        return self._process is not None and self._process.poll() is None
