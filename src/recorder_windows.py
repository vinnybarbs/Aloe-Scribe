"""
recorder_windows.py — Windows audio capture for Aloe Scribe.

Captures the microphone and the system output (what you hear) at the same time
and mixes them into a single 16 kHz mono WAV, the same output contract the rest
of the app expects. System audio uses WASAPI loopback, which is built into
Windows 10/11 — no virtual audio cable or extra driver needed.

Unlike the Mac path there is no separate helper binary. Capture runs in-process
on two threads (one per device) via PyAudioWPatch. Each thread downmixes to
mono and resamples to 16 kHz on the fly and writes its own temp WAV, so memory
stays flat on long meetings. At Stop the two temp tracks are summed into the
final WAV and deleted.

Interface mirrors recorder_mac.py: list_sources(), Recorder(mic_source,
system_source), start(path)->bool, stop()->Optional[Path], is_recording()->bool.
"""

import logging
import threading
import wave
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

TARGET_RATE = 16000          # Hz, mono — matches the transcriber's expected input
CHUNK_FRAMES = 1600          # ~100 ms per read at most native rates
PREVIEW_RING_SEC = 60        # keep this many seconds of recent audio for the live preview


def _pyaudio():
    """Import PyAudioWPatch lazily so the module imports even where it's absent
    (e.g. a dev box). Returns the module or None."""
    try:
        import pyaudiowpatch as pyaudio
        return pyaudio
    except ImportError as e:
        log.error(
            "PyAudioWPatch not installed. Run: "
            ".venv\\Scripts\\pip install PyAudioWPatch"
        )
        log.error(f"  underlying error: {e}")
        return None


# ---------------------------------------------------------------------------
# Device enumeration
# ---------------------------------------------------------------------------

def _wasapi_input_devices(p) -> list[dict]:
    """All real WASAPI input devices (microphones), excluding loopback
    capture endpoints (those are system-audio, listed separately)."""
    pyaudio = _pyaudio()
    devices = []
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    except Exception:
        return devices
    api_index = wasapi["index"]
    for i in range(p.get_device_count()):
        try:
            info = p.get_device_info_by_index(i)
        except Exception:
            continue
        if info.get("hostApi") != api_index:
            continue
        if info.get("maxInputChannels", 0) <= 0:
            continue
        # PyAudioWPatch flags loopback capture endpoints; skip them here.
        if info.get("isLoopbackDevice", False):
            continue
        devices.append(info)
    return devices


def list_sources() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """
    List audio sources for the UI dropdowns.
    Returns (mic_sources, system_sources) as lists of (id, display_name).

    Mic id == name for a stable identifier across reboots / index shuffles,
    matching the Mac behavior. System is one synthetic option — WASAPI loopback
    always captures the default output's mix.
    """
    pyaudio = _pyaudio()
    mics: list[tuple[str, str]] = []
    if pyaudio is not None:
        p = pyaudio.PyAudio()
        try:
            for info in _wasapi_input_devices(p):
                name = info.get("name", "").strip()
                if name:
                    mics.append((name, name))
        finally:
            p.terminate()
    system = [("system", "System Audio (built-in)")]
    return mics, system


# ---------------------------------------------------------------------------
# Resampling / mixing helpers
# ---------------------------------------------------------------------------

def _to_mono_16k(samples, channels: int, src_rate: int):
    """Downmix interleaved float32 samples to mono and resample to 16 kHz.
    Returns a 1-D float32 numpy array. Per-chunk linear resampling — adequate
    for speech, and avoids a scipy dependency."""
    import numpy as np

    if samples.size == 0:
        return samples.astype(np.float32)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if src_rate == TARGET_RATE:
        return samples.astype(np.float32)
    n_out = int(round(samples.shape[0] * TARGET_RATE / src_rate))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    x_old = np.linspace(0.0, 1.0, num=samples.shape[0], endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

class Recorder:
    """
    Records mic + system audio into a single 16 kHz mono WAV via WASAPI.

    `mic_source`: WASAPI input device name (e.g. "Microphone (Realtek)"), or
        empty/auto for the default input.
    `system_source`: kept for UI/config compatibility. Treated as a boolean —
        falsy ("", "off", "none") disables loopback capture; anything else
        (including the synthetic "system" id) enables it.
    """

    def __init__(self, mic_source: str = "", system_source: str = ""):
        self._mic_config = mic_source
        self._sys_config = system_source
        self._output_path: Optional[Path] = None
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._mic_tmp: Optional[Path] = None
        self._sys_tmp: Optional[Path] = None
        self._running = False
        # Rolling buffers of recent 16 kHz mono float32 audio, one per source,
        # for the live preview. Capped to PREVIEW_RING_SEC so memory stays flat.
        self._buf_lock = threading.Lock()
        self._rings: dict = {"mic": None, "sys": None}

    def _capture_system(self) -> bool:
        v = (self._sys_config or "").strip().lower()
        return v not in {"off", "none", "false", "0", "no", "disabled"}

    def _find_mic_info(self, p):
        """Resolve the configured mic name to a device info dict, else the
        default WASAPI input."""
        if self._mic_config:
            for info in _wasapi_input_devices(p):
                if info.get("name", "") == self._mic_config:
                    return info
            log.warning(
                f"Mic '{self._mic_config}' not found — using default input."
            )
        try:
            return p.get_default_input_device_info()
        except Exception:
            devs = _wasapi_input_devices(p)
            return devs[0] if devs else None

    def _find_loopback_info(self, p):
        """The loopback capture endpoint for the default output device."""
        try:
            return p.get_default_wasapi_loopback()
        except Exception as e:
            log.warning(f"No default WASAPI loopback device: {e}")
            return None

    def _capture_thread(self, device_info, tmp_path: Path, tag: str):
        """Read one device to 16 kHz mono int16 WAV until stop is set, and feed
        the same audio into the rolling preview ring for this source (tag)."""
        import numpy as np

        pyaudio = _pyaudio()
        p = pyaudio.PyAudio()
        stream = None
        wav = None
        try:
            channels = int(device_info.get("maxInputChannels", 1)) or 1
            src_rate = int(device_info.get("defaultSampleRate", 48000)) or 48000
            stream = p.open(
                format=pyaudio.paFloat32,
                channels=channels,
                rate=src_rate,
                input=True,
                input_device_index=int(device_info["index"]),
                frames_per_buffer=CHUNK_FRAMES,
            )
            wav = wave.open(str(tmp_path), "wb")
            wav.setnchannels(1)
            wav.setsampwidth(2)  # int16
            wav.setframerate(TARGET_RATE)

            while not self._stop_event.is_set():
                try:
                    raw = stream.read(CHUNK_FRAMES, exception_on_overflow=False)
                except Exception as e:
                    log.debug(f"stream.read: {e}")
                    continue
                if not raw:
                    continue
                floats = np.frombuffer(raw, dtype=np.float32)
                mono16k = _to_mono_16k(floats, channels, src_rate)
                if mono16k.size:
                    clipped = np.clip(mono16k, -1.0, 1.0)
                    wav.writeframes((clipped * 32767.0).astype(np.int16).tobytes())
                    # Feed the preview ring (bounded to PREVIEW_RING_SEC).
                    cap = PREVIEW_RING_SEC * TARGET_RATE
                    with self._buf_lock:
                        prev = self._rings.get(tag)
                        merged = clipped if prev is None else np.concatenate([prev, clipped])
                        self._rings[tag] = merged[-cap:]
        except Exception as e:
            log.error(f"Capture thread failed ({tmp_path.name}): {e}")
        finally:
            try:
                if stream is not None:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass
            try:
                if wav is not None:
                    wav.close()
            except Exception:
                pass
            p.terminate()

    def start(self, output_path: Path) -> bool:
        """Start recording to output_path. Returns True if started."""
        if self._running:
            log.warning("Already recording")
            return False

        pyaudio = _pyaudio()
        if pyaudio is None:
            return False

        p = pyaudio.PyAudio()
        try:
            mic_info = self._find_mic_info(p)
            sys_info = self._find_loopback_info(p) if self._capture_system() else None
        finally:
            p.terminate()

        if mic_info is None and sys_info is None:
            log.error("Refusing to record: no mic and no system audio")
            return False

        self._output_path = output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._threads = []
        with self._buf_lock:
            self._rings = {"mic": None, "sys": None}

        # Temp per-device tracks live next to the final file.
        stem = output_path.stem
        self._mic_tmp = output_path.with_name(f".{stem}.mic.wav") if mic_info else None
        self._sys_tmp = output_path.with_name(f".{stem}.sys.wav") if sys_info else None

        if mic_info is not None:
            log.info(f"Mic capture: {mic_info.get('name')}")
            t = threading.Thread(
                target=self._capture_thread,
                args=(mic_info, self._mic_tmp, "mic"),
                daemon=True,
            )
            t.start()
            self._threads.append(t)
        if sys_info is not None:
            log.info(f"System loopback capture: {sys_info.get('name')}")
            t = threading.Thread(
                target=self._capture_thread,
                args=(sys_info, self._sys_tmp, "sys"),
                daemon=True,
            )
            t.start()
            self._threads.append(t)

        if not self._threads:
            log.error("No capture threads started")
            return False
        self._running = True
        return True

    def stop(self) -> Optional[Path]:
        """Stop recording, mix the tracks, return the final WAV path."""
        if not self._running:
            log.warning("Not recording")
            return None

        log.info("Stopping recorder...")
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=15)
        self._threads = []
        self._running = False

        try:
            self._mix_tracks()
        except Exception as e:
            log.error(f"Mixing tracks failed: {e}")
        finally:
            for tmp in (self._mic_tmp, self._sys_tmp):
                try:
                    if tmp and tmp.exists():
                        tmp.unlink()
                except Exception:
                    pass

        log.info(f"Recording saved: {self._output_path}")
        return self._output_path

    def _mix_tracks(self):
        """Sum the per-device 16 kHz mono temp tracks into the final WAV.
        Both tracks share rate/width/channels, so mixing is a chunked int32 sum
        with clipping — bounded memory regardless of meeting length."""
        import numpy as np

        tracks = [t for t in (self._mic_tmp, self._sys_tmp) if t and t.exists()]
        if not tracks:
            # Nothing captured — leave a valid empty WAV so downstream code can
            # detect "too small" cleanly rather than crash on a missing file.
            with wave.open(str(self._output_path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(TARGET_RATE)
            return

        readers = [wave.open(str(t), "rb") for t in tracks]
        try:
            out = wave.open(str(self._output_path), "wb")
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(TARGET_RATE)
            try:
                frames_per_read = TARGET_RATE  # 1 s
                while True:
                    chunks = [r.readframes(frames_per_read) for r in readers]
                    if all(len(c) == 0 for c in chunks):
                        break
                    arrays = [
                        np.frombuffer(c, dtype=np.int16).astype(np.int32)
                        for c in chunks
                    ]
                    n = max((a.shape[0] for a in arrays), default=0)
                    mixed = np.zeros(n, dtype=np.int32)
                    for a in arrays:
                        if a.shape[0] < n:
                            a = np.pad(a, (0, n - a.shape[0]))
                        mixed += a
                    mixed = np.clip(mixed, -32768, 32767).astype(np.int16)
                    out.writeframes(mixed.tobytes())
            finally:
                out.close()
        finally:
            for r in readers:
                try:
                    r.close()
                except Exception:
                    pass

    def snapshot_recent(self, seconds: float):
        """Return the last `seconds` of mixed mic+system audio as a 16 kHz mono
        float32 numpy array (values in -1..1), for the live preview. Returns None
        if nothing has been captured yet."""
        import numpy as np

        n = int(seconds * TARGET_RATE)
        with self._buf_lock:
            parts = [r for r in (self._rings.get("mic"), self._rings.get("sys")) if r is not None]
            if not parts:
                return None
            tails = [p[-n:].astype(np.float32) for p in parts]

        length = max(t.shape[0] for t in tails)
        if length == 0:
            return None
        mixed = np.zeros(length, dtype=np.float32)
        for t in tails:
            if t.shape[0] < length:
                t = np.pad(t, (length - t.shape[0], 0))  # left-pad so the ends align
            mixed += t
        return np.clip(mixed, -1.0, 1.0)

    def is_recording(self) -> bool:
        return self._running
