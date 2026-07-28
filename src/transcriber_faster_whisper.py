"""
transcriber_faster_whisper.py — faster-whisper backend for Aloe Scribe (Windows).

Mirrors the external interface of ParakeetTranscriber and the whisper.cpp
Transcriber so main.py can pick a backend by config without touching the rest
of the app. The transcript it writes is byte-for-byte the same shape as the
Mac and iOS writers: a machine-readable YAML header followed by timestamped
lines, no prose.

Why faster-whisper on Windows: Parakeet runs on Apple MLX, which is Apple
Silicon only and cannot run on Windows at all. faster-whisper (CTranslate2)
is cross-platform, pip-installable, runs on CPU or NVIDIA CUDA, and loads the
model straight from a local folder — so the install can ship the weights from
GitHub and never call Hugging Face at runtime.

This backend does batch transcription at Stop. It does NOT implement the
cache-aware live streaming the Parakeet backend has (Whisper has no equivalent
real-time API), so main.py's streaming path stays Mac-only and the Windows app
transcribes when you press Stop.
"""

import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from frontmatter import build_frontmatter

log = logging.getLogger(__name__)


class FasterWhisperTranscriber:
    """
    Drop-in replacement for transcriber.Transcriber / ParakeetTranscriber.
    Same transcribe() signature, so swapping backends is config-only.
    """

    # CTranslate2 Whisper model. Can be a local directory (the install fetches
    # the weights from GitHub, not Hugging Face) or a model id. A local dir is
    # the default on a packaged Windows install.
    DEFAULT_MODEL = "Systran/faster-distil-whisper-large-v3"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        cache_dir: Optional[str] = None,
        device: str = "auto",
        compute_type: Optional[str] = None,
        language: str = "en",
    ):
        self.model_name = model
        self.cache_dir = cache_dir
        self.language = language
        # device="auto" lets CTranslate2 pick CUDA when an NVIDIA GPU is
        # present and fall back to CPU otherwise — the "mixed fleet" case.
        self.device = device
        # int8 on CPU is the sweet spot for accuracy vs speed; float16 on GPU.
        # Left to resolve at load time once the real device is known.
        self.compute_type = compute_type
        self._model = None
        self._load_lock = threading.Lock()
        # Serializes model.transcribe calls so the live-preview pass and the
        # final pass never run on the model at the same time.
        self._infer_lock = threading.Lock()

    def _resolve_device_and_compute(self):
        """Pick a concrete (device, compute_type) pair. CUDA when available,
        else CPU. Honors explicit overrides passed to __init__."""
        device = self.device
        if device == "auto":
            try:
                import ctranslate2

                if ctranslate2.get_cuda_device_count() > 0:
                    device = "cuda"
                else:
                    device = "cpu"
            except Exception:
                device = "cpu"
        compute_type = self.compute_type
        if compute_type is None:
            compute_type = "float16" if device == "cuda" else "int8"
        return device, compute_type

    def _ensure_loaded(self) -> bool:
        """Lazy-load the model. Returns False on failure."""
        if self._model is not None:
            return True
        with self._load_lock:
            if self._model is not None:
                return True
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                log.error(
                    "faster-whisper not installed. Run: "
                    ".venv\\Scripts\\pip install faster-whisper"
                )
                log.error(f"  underlying error: {e}")
                return False

            device, compute_type = self._resolve_device_and_compute()
            log.info(
                f"Loading faster-whisper model: {self.model_name} "
                f"(device={device}, compute_type={compute_type})"
            )
            try:
                kwargs = {"device": device, "compute_type": compute_type}
                if self.cache_dir:
                    kwargs["download_root"] = self.cache_dir
                self._model = WhisperModel(self.model_name, **kwargs)
            except Exception as e:
                log.error(
                    f"Failed to load faster-whisper model {self.model_name}: {e}"
                )
                # A CUDA load can fail on a machine without the CUDA runtime even
                # though a GPU exists. Retry once on CPU before giving up.
                if device == "cuda":
                    try:
                        log.warning("Retrying model load on CPU...")
                        self._model = WhisperModel(
                            self.model_name, device="cpu", compute_type="int8"
                        )
                    except Exception as e2:
                        log.error(f"CPU fallback also failed: {e2}")
                        return False
                else:
                    return False
            log.info("faster-whisper model ready")
            return True

    def preload(self):
        """Load the model ahead of time (called when recording starts) so the
        transcribe at Stop is fast. Best-effort."""
        try:
            self._ensure_loaded()
        except Exception:
            pass

    def transcribe(
        self,
        audio_path: Path,
        output_path: Path,
        meeting_title: str,
        meeting_date: datetime,
    ) -> Optional[Path]:
        """Transcribe audio_path and write Markdown to output_path."""
        log.info(f"Transcribing (faster-whisper): {audio_path.name}")
        if not audio_path.exists():
            log.error(f"Audio file does not exist: {audio_path}")
            return None
        # A bare WAV header (no samples) means nothing was captured. Bail with a
        # clean log instead of handing the decoder an empty file.
        try:
            if audio_path.stat().st_size < 1024:
                log.warning(
                    f"Audio file too small to transcribe "
                    f"({audio_path.stat().st_size} bytes): {audio_path}"
                )
                return None
        except Exception:
            pass

        if not self._ensure_loaded():
            return None

        try:
            with self._infer_lock:
                segments, _info = self._model.transcribe(
                    str(audio_path),
                    language=self.language,
                    beam_size=5,
                    vad_filter=True,
                )
                # segments is a generator — materialize it under the lock so the
                # whole decode finishes before another pass can start.
                segments = list(segments)
        except Exception as e:
            log.error(f"faster-whisper transcription failed: {e}")
            return None

        markdown = self._build_markdown(segments, meeting_title, meeting_date)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
        log.info(f"Transcript saved: {output_path}")
        return output_path

    def transcribe_sentences(self, audio_path: Path) -> Optional[list]:
        """Batch-transcribe to timestamped segments as
        [{'start': s, 'end': s, 'text': str}, ...] — the shape the speaker
        labeler consumes. The audio should be mono (callers with a split WAV
        downmix first). None on failure."""
        if not audio_path.exists() or not self._ensure_loaded():
            return None
        try:
            with self._infer_lock:
                segments, _info = self._model.transcribe(
                    str(audio_path),
                    language=self.language,
                    beam_size=5,
                    vad_filter=True,
                )
                segments = list(segments)
        except Exception as e:
            log.error(f"faster-whisper transcription failed: {e}")
            return None
        return [
            {
                "start": float(getattr(s, "start", 0) or 0),
                "end": float(getattr(s, "end", 0) or 0),
                "text": (getattr(s, "text", "") or "").strip(),
            }
            for s in segments
            if (getattr(s, "text", "") or "").strip()
        ]

    def transcribe_samples(self, samples) -> str:
        """Transcribe an in-memory 16 kHz mono float32 array (values in -1..1)
        and return the plain text. Used for the live preview, which re-transcribes
        a rolling window of recent audio. Best-effort: returns '' on failure.
        Greedy decode (beam_size=1) keeps it fast enough to feel live."""
        if samples is None or len(samples) == 0:
            return ""
        if not self._ensure_loaded():
            return ""
        try:
            with self._infer_lock:
                segments, _info = self._model.transcribe(
                    samples,
                    language=self.language,
                    beam_size=1,
                    vad_filter=True,
                )
                return " ".join(
                    (getattr(s, "text", "") or "").strip() for s in segments
                ).strip()
        except Exception as e:
            log.debug(f"transcribe_samples: {e}")
            return ""

    def _build_markdown(self, segments, title: str, date: datetime) -> str:
        """Build the transcript file: a machine-readable YAML header, then the
        timestamped transcript. No prose — this file is a handoff for a
        downstream agent to enrich and summarize, so it carries data, not
        formatting. Output shape matches the Parakeet and iOS writers exactly."""
        duration_s = max(
            (float(getattr(s, "end", 0) or 0) for s in segments),
            default=0.0,
        )

        lines = [
            build_frontmatter(title, date, duration_s, source="aloe-scribe-windows"),
            "",
        ]

        for s in segments:
            text = (getattr(s, "text", "") or "").strip()
            if not text:
                continue
            start_s = int(getattr(s, "start", 0) or 0)
            m, sec = divmod(start_s, 60)
            lines.append(f"[{m:02d}:{sec:02d}] {text}")

        return "\n".join(lines) + "\n"
