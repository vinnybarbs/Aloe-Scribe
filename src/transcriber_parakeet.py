"""
transcriber_parakeet.py — Parakeet TDT backend for Aloe Scribe.

Provides ParakeetTranscriber with the same external interface as the
whisper.cpp-based Transcriber, so main.py can swap backends via config
without touching the rest of the codebase.

Wraps `parakeet-mlx`, NVIDIA Parakeet TDT models compiled for Apple Silicon
through MLX. Tends to be ~2–3× faster than whisper-large-v3-turbo on the
same hardware and is much less prone to silence-time hallucinations / the
"repeat the last phrase forever" failure mode whisper has.

Model is downloaded from HuggingFace on first use (cached in
~/.cache/huggingface/) and loaded lazily on the first transcribe() call so
app startup isn't blocked by the ~1.2 GB weight load.
"""

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from frontmatter import build_frontmatter

log = logging.getLogger(__name__)


class ParakeetTranscriber:
    """
    Drop-in replacement for transcriber.Transcriber. Shares the same
    transcribe() signature so swapping backends is config-only.
    """

    DEFAULT_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"

    def __init__(self, model: str = DEFAULT_MODEL, cache_dir: Optional[str] = None):
        self.model_name = model
        self.cache_dir = cache_dir
        self._model = None
        self._load_lock = threading.Lock()
        # Serializes model inference so the live-preview loop and the final
        # transcription never call the (non-reentrant) MLX model concurrently.
        self._infer_lock = threading.Lock()

    def _ensure_loaded(self) -> bool:
        """Lazy-load the Parakeet model. Returns False on failure."""
        if self._model is not None:
            return True
        with self._load_lock:
            if self._model is not None:
                return True
            try:
                import parakeet_mlx  # heavy import — keep inside the lock
            except ImportError as e:
                log.error(
                    "parakeet-mlx not installed. Run: "
                    ".venv/bin/pip install parakeet-mlx"
                )
                log.error(f"  underlying error: {e}")
                return False
            log.info(f"Loading Parakeet model: {self.model_name}")
            try:
                self._model = parakeet_mlx.from_pretrained(
                    self.model_name, cache_dir=self.cache_dir
                )
            except Exception as e:
                log.error(f"Failed to load Parakeet model {self.model_name}: {e}")
                return False
            log.info("Parakeet model ready")
            return True

    def transcribe(
        self,
        audio_path: Path,
        output_path: Path,
        meeting_title: str,
        meeting_date: datetime,
    ) -> Optional[Path]:
        """Transcribe audio_path and write Markdown to output_path."""
        log.info(f"Transcribing (parakeet): {audio_path.name}")
        if not audio_path.exists():
            log.error(f"Audio file does not exist: {audio_path}")
            return None
        # Parakeet's frontend chokes on bare WAV-header files (no samples). Bail
        # before invoking it so we can surface a clean error instead of a Metal
        # buffer-size overflow.
        try:
            if audio_path.stat().st_size < 1024:
                log.warning(
                    f"Audio file too small to transcribe ({audio_path.stat().st_size} bytes): "
                    f"{audio_path}"
                )
                return None
        except Exception:
            pass

        if not self._ensure_loaded():
            return None

        try:
            # chunk_duration=120s with 15s overlap keeps long meetings under
            # Parakeet's max input window while preserving context across the
            # seams. Tuned per parakeet-mlx README guidance.
            with self._infer_lock:
                result = self._model.transcribe(
                    str(audio_path),
                    chunk_duration=120.0,
                    overlap_duration=15.0,
                )
        except Exception as e:
            log.error(f"Parakeet transcription failed: {e}")
            return None

        markdown = self._build_markdown(result, meeting_title, meeting_date)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
        log.info(f"Transcript saved: {output_path}")
        return output_path

    def preload(self):
        """Load the model ahead of time (called when recording starts) so live
        streaming starts the instant the first audio arrives. Best-effort."""
        try:
            self._ensure_loaded()
        except Exception:
            pass

    def new_stream(self, depth: int = 2):
        """Return a StreamingParakeet context manager for real-time (cache-aware)
        transcription — `with t.new_stream() as s: s.add_audio(mx_array)`; read
        `s.result` for the growing transcript. Returns None if the model can't
        load. `depth` trades fidelity vs. cost (higher = closer to a full pass)."""
        if not self._ensure_loaded():
            return None
        return self._model.transcribe_stream(depth=depth)

    def markdown(self, result, title: str, date: datetime) -> str:
        """Build the Markdown transcript text from a result object (the live
        stream's `.result`, or a batch transcription result)."""
        return self._build_markdown(result, title, date)

    def _build_markdown(self, result, title: str, date: datetime) -> str:
        date_str = date.strftime("%B %d, %Y")
        time_str = date.strftime("%I:%M %p")

        sentences_for_dur = getattr(result, "sentences", None) or []
        duration_s = max(
            (float(getattr(s, "end", 0) or 0) for s in sentences_for_dur),
            default=0.0,
        )

        lines = [
            build_frontmatter(title, date, duration_s, source="aloe-scribe-mac"),
            f"# {title}",
            f"**Date:** {date_str}  ",
            f"**Time:** {time_str}  ",
            f"**Transcribed by:** Aloe Scribe (Parakeet)  ",
            "",
            "---",
            "",
            "## Full Transcript",
            "",
        ]

        sentences = getattr(result, "sentences", None) or []
        if not sentences:
            text = getattr(result, "text", "").strip()
            if text:
                lines.append(text)
            else:
                lines.append("_No speech detected._")
        else:
            for s in sentences:
                start_s = int(getattr(s, "start", 0) or 0)
                m, sec = divmod(start_s, 60)
                stamp = f"{m:02d}:{sec:02d}"
                text = (getattr(s, "text", "") or "").strip()
                if not text:
                    continue
                lines.append(f"`{stamp}` {text}")
                lines.append("")

        lines += [
            "---",
            "",
            f"_Transcript generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · Aloe Scribe_",
        ]
        return "\n".join(lines)
