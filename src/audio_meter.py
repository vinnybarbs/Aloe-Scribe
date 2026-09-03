"""
audio_meter.py — Live audio level reader.

Spawns a subprocess that streams 16-bit signed little-endian PCM at 16 kHz
mono on stdout, computes the absolute peak of each ~100 ms chunk, and reports
it via a callback. Used by the GTK and PySide6 UIs to draw VU bars next to the
device dropdowns and during recording.

Two factories build the right command for the current platform:
  - make_pulseaudio_meter(source, on_level)        — Linux (parec)
  - make_avfoundation_meter(device_idx, on_level)  — macOS (ffmpeg)
"""

import logging
import struct
import subprocess
import sys
import threading
from typing import Callable, List, Optional

log = logging.getLogger(__name__)


class AudioMeter:
    RATE = 16000
    CHUNK_MS = 100
    CHUNK_SAMPLES = (RATE * CHUNK_MS) // 1000
    CHUNK_BYTES = CHUNK_SAMPLES * 2  # s16le = 2 bytes/sample

    def __init__(self, cmd: List[str], on_level: Callable[[float], None]):
        """cmd: subprocess command that writes raw s16le 16kHz mono PCM to stdout."""
        self.cmd = cmd
        self.on_level = on_level
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> bool:
        if self._proc is not None:
            return True
        if not self.cmd:
            return False
        try:
            # We pipe stdin (instead of /dev/null) because the aloe-audio-capture
            # helper interprets stdin EOF as "stop" — a closed stdin would kill
            # the meter the instant it starts. Holding the pipe open here keeps
            # the helper alive for the lifetime of this AudioMeter.
            self._proc = subprocess.Popen(
                self.cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.PIPE,
            )
        except FileNotFoundError:
            log.warning(f"Command not found: {self.cmd[0]} — meter disabled")
            return False
        except Exception as e:
            log.warning(f"Failed to start meter ({self.cmd[0]}): {e}")
            return False

        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        proc = self._proc
        self._proc = None
        if proc is not None:
            # Close stdin first so helpers that watch for EOF (e.g. the
            # aloe-audio-capture meter mode) can shut down cleanly.
            try:
                if proc.stdin is not None and not proc.stdin.closed:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._thread = None
        try:
            self.on_level(0.0)
        except Exception:
            pass

    def _loop(self):
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        while not self._stop.is_set():
            try:
                buf = proc.stdout.read(self.CHUNK_BYTES)
            except Exception:
                break
            if not buf:
                break
            n = len(buf) // 2
            if n == 0:
                continue
            samples = struct.unpack(f"<{n}h", buf[: n * 2])
            peak = max(abs(s) for s in samples) / 32768.0
            try:
                self.on_level(peak)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Platform factories
# ---------------------------------------------------------------------------

def make_pulseaudio_meter(source: str, on_level: Callable[[float], None]) -> Optional[AudioMeter]:
    """Linux: read a PulseAudio source name via parec."""
    if not source:
        return None
    cmd = [
        "parec",
        f"--device={source}",
        "--format=s16le",
        f"--rate={AudioMeter.RATE}",
        "--channels=1",
        "--latency-msec=50",
        "--raw",
    ]
    return AudioMeter(cmd, on_level)


def make_avfoundation_meter(device_spec: str, on_level: Callable[[float], None]) -> Optional[AudioMeter]:
    """macOS: read an avfoundation device via ffmpeg. `device_spec` is
    ':<name>' or ':<index>' — passing the NAME avoids the device enumeration
    that index resolution needs (enumeration wakes Continuity phone mics)."""
    if not device_spec:
        return None
    # ffmpeg writes raw s16le PCM to stdout. -nostdin so it doesn't fight the
    # parent for tty/stdin input. -loglevel error keeps stderr quiet.
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-loglevel", "error",
        "-f", "avfoundation",
        "-i", device_spec,
        "-ar", str(AudioMeter.RATE),
        "-ac", "1",
        "-f", "s16le",
        "-",
    ]
    return AudioMeter(cmd, on_level)


def make_sck_meter(helper_path: str, on_level: Callable[[float], None]) -> Optional[AudioMeter]:
    """macOS: read ScreenCaptureKit system audio via the aloe-audio-capture
    helper in --meter mode. The helper writes raw s16le mono 16 kHz PCM to
    stdout — same format the AudioMeter base class expects.

    Returns None if the helper binary isn't available so callers can fall back
    to no system meter gracefully.
    """
    import os
    if not helper_path or not os.access(helper_path, os.X_OK):
        return None
    cmd = [helper_path, "--meter", "--sample-rate", str(AudioMeter.RATE)]
    return AudioMeter(cmd, on_level)


def make_meter_for_platform(source: str, on_level: Callable[[float], None]) -> Optional[AudioMeter]:
    """Convenience: pick the right factory for the current platform.

    On Linux, `source` is a PulseAudio source name.
    On macOS, `source` is an avfoundation device index string like ':2'."""
    if sys.platform == "darwin":
        return make_avfoundation_meter(source, on_level)
    return make_pulseaudio_meter(source, on_level)
