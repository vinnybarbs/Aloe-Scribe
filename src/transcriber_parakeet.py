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
from concurrent.futures import ThreadPoolExecutor
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
        # MLX ties its GPU stream to the thread that first uses the model, so
        # every model call (load, transcribe, streaming) MUST run on one thread.
        # This single-worker executor is that thread. It also serializes the
        # live stream and the final transcription, so they never collide.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aloe-infer")
        self._stream_cm = None
        self._stream = None

    def _run(self, fn, *args, **kwargs):
        """Run a model operation on the single inference thread and wait."""
        return self._executor.submit(fn, *args, **kwargs).result()

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
                missing = getattr(e, "name", "") or ""
                if missing in ("parakeet_mlx", "mlx"):
                    log.error(
                        "parakeet-mlx not installed. Run: "
                        ".venv/bin/pip install parakeet-mlx"
                    )
                else:
                    # A dependency or stdlib module is missing — common in a
                    # frozen py2app bundle that dropped a lazily-imported
                    # stdlib module (e.g. 'tty' via click). This is NOT the
                    # same as parakeet-mlx being absent; say so plainly.
                    log.error(
                        f"Parakeet import failed: missing module "
                        f"'{missing or 'unknown'}'. parakeet-mlx itself may be "
                        f"installed; a dependency could not load."
                    )
                log.error(f"  underlying error: {e}")
                return False
            log.info(f"Loading Parakeet model: {self.model_name}")
            try:
                # Load on the inference thread so the GPU stream lives there.
                self._model = self._run(
                    parakeet_mlx.from_pretrained,
                    self.model_name,
                    cache_dir=self.cache_dir,
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
            # seams. Run on the inference thread (see __init__).
            result = self._run(
                self._model.transcribe,
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

    # --- Live streaming (cache-aware). All MLX work runs on the inference
    # thread via _run, so the stream is created, fed, and closed on the same
    # thread that loaded the model. ---

    def stream_open(self, depth: int = 2) -> bool:
        """Open a streaming session. Returns False if the model can't load."""
        if not self._ensure_loaded():
            return False

        def _open():
            import mlx.core as mx  # noqa: F401 (ensures MLX is live on this thread)
            cm = self._model.transcribe_stream(depth=depth)
            stream = cm.__enter__()
            return cm, stream

        try:
            self._stream_cm, self._stream = self._run(_open)
            return True
        except Exception as e:
            log.warning(f"Could not open stream: {e}")
            self._stream_cm = self._stream = None
            return False

    def stream_feed(self, samples) -> str:
        """Feed one chunk of 16 kHz mono float32 samples (a numpy array) and
        return the growing transcript text. Best-effort, returns '' on failure."""
        if self._stream is None:
            return ""

        def _feed():
            import mlx.core as mx
            self._stream.add_audio(mx.array(samples))
            return (getattr(self._stream.result, "text", "") or "").strip()

        try:
            return self._run(_feed)
        except Exception as e:
            log.debug(f"stream_feed: {e}")
            return ""

    def stream_text(self) -> str:
        """Plain transcript text from the current stream (to check it worked)."""
        if self._stream is None:
            return ""
        try:
            return self._run(
                lambda: (getattr(self._stream.result, "text", "") or "").strip()
            )
        except Exception:
            return ""

    def stream_markdown(self, title: str, date: datetime) -> str:
        """Build the transcript markdown from the current stream result."""
        if self._stream is None:
            return ""
        try:
            return self._run(self._build_markdown, self._stream.result, title, date)
        except Exception:
            return ""

    def stream_close(self):
        """Close the streaming session."""
        cm = self._stream_cm
        self._stream_cm = self._stream = None
        if cm is None:
            return

        def _close():
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass

        try:
            self._run(_close)
        except Exception:
            pass

    def _build_markdown(self, result, title: str, date: datetime) -> str:
        """Build the transcript file: a machine-readable YAML header, then the
        timestamped transcript. No prose. This file is a handoff for a
        downstream agent to enrich and summarize, so it carries data, not
        formatting."""
        sentences = getattr(result, "sentences", None) or []
        duration_s = max(
            (float(getattr(s, "end", 0) or 0) for s in sentences),
            default=0.0,
        )

        lines = [build_frontmatter(title, date, duration_s, source="aloe-scribe-mac"), ""]

        if not sentences:
            text = (getattr(result, "text", "") or "").strip()
            if text:
                lines.append(text)
        else:
            for s in sentences:
                text = (getattr(s, "text", "") or "").strip()
                if not text:
                    continue
                start_s = int(getattr(s, "start", 0) or 0)
                m, sec = divmod(start_s, 60)
                lines.append(f"[{m:02d}:{sec:02d}] {text}")

        return "\n".join(lines) + "\n"
