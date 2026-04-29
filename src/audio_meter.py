"""
audio_meter.py — Live PulseAudio level reader for a single source.

Spawns `parec` as a subprocess and reports peak levels (0.0–1.0) at ~10 Hz
via a callback. PulseAudio sources support multiple simultaneous readers,
so this can run alongside the recorder without disrupting it.
"""

import logging
import struct
import subprocess
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)


class AudioMeter:
    RATE = 16000
    CHUNK_MS = 100
    CHUNK_SAMPLES = (RATE * CHUNK_MS) // 1000
    CHUNK_BYTES = CHUNK_SAMPLES * 2  # s16le = 2 bytes/sample

    def __init__(self, source: str, on_level: Callable[[float], None]):
        self.source = source
        self.on_level = on_level
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> bool:
        if self._proc is not None:
            return True
        if not self.source:
            return False
        cmd = [
            "parec",
            f"--device={self.source}",
            "--format=s16le",
            f"--rate={self.RATE}",
            "--channels=1",
            "--latency-msec=50",
            "--raw",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            log.warning("parec not found — audio meters disabled")
            return False
        except Exception as e:
            log.warning(f"Failed to start meter for {self.source}: {e}")
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
