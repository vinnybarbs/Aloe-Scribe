"""
transcriber.py — Runs whisper.cpp on a WAV file and returns a structured Markdown transcript.

Handles speaker diarization labelling and formats the output ready to land in SharePoint.
"""

import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from frontmatter import build_frontmatter

log = logging.getLogger(__name__)


class TranscriptSegment:
    def __init__(self, start_ms: int, end_ms: int, text: str, speaker: str = ""):
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.text = text.strip()
        self.speaker = speaker

    def timestamp(self) -> str:
        total_s = self.start_ms // 1000
        m = total_s // 60
        s = total_s % 60
        return f"{m:02d}:{s:02d}"


class Transcriber:
    """
    Wraps whisper.cpp to transcribe a WAV file.
    Parses the output into structured segments and writes a Markdown file.
    """

    def __init__(self, binary_path: str, model_path: str):
        self.binary_path = os.path.expanduser(binary_path)
        self.model_path = os.path.expanduser(model_path)

    def transcribe(
        self,
        audio_path: Path,
        output_path: Path,
        meeting_title: str,
        meeting_date: datetime,
    ) -> Optional[Path]:
        """
        Transcribe audio_path and write Markdown to output_path.
        Returns output_path on success, None on failure.
        """
        log.info(f"Transcribing: {audio_path.name}")

        raw_output = self._run_whisper(audio_path)
        if raw_output is None:
            return None

        segments = self._parse_output(raw_output)
        markdown = self._build_markdown(segments, meeting_title, meeting_date)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown, encoding="utf-8")
        log.info(f"Transcript saved: {output_path}")
        return output_path

    def _run_whisper(self, audio_path: Path) -> Optional[str]:
        """Run whisper.cpp and return raw stdout output."""
        if not os.path.exists(self.binary_path):
            log.error(
                f"whisper.cpp binary not found at {self.binary_path}\n"
                "Build it from: https://github.com/ggerganov/whisper.cpp"
            )
            return None

        if not os.path.exists(self.model_path):
            log.error(
                f"Whisper model not found at {self.model_path}\n"
                "Download with: bash models/download-ggml-model.sh base"
            )
            return None

        # whisper.cpp defaults to 4 threads. On a modern 8+ core CPU, 8 threads
        # cuts compute time roughly in half. Going past physical core count is
        # counter-productive (SMT contention slows the encoder), so we cap at
        # 8 unless the user has fewer logical cores.
        threads = min(8, max(1, (os.cpu_count() or 4)))

        # -mc 0  : do not carry previous-window text forward as context. Without
        #          this, whisper's "condition on previous text" causes a single
        #          hallucination during silence to cascade into the same phrase
        #          repeating dozens of times.
        # -sns   : suppress non-speech tokens. Stops whisper from filling silence
        #          with "Okay.", "Thank you.", "[Music]", etc.
        cmd = [
            self.binary_path,
            "-m", self.model_path,
            "-f", str(audio_path),
            "-t", str(threads),
            "-l", "en",
            "-mc", "0",
            "-sns",
        ]

        log.info(f"Running: {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600,  # 1 hour max
            )
            if result.returncode != 0:
                log.error(f"whisper.cpp error: {result.stderr}")
                return None
            # Log output for debugging
            log.info(f"Whisper stdout length: {len(result.stdout)} chars")
            if not result.stdout.strip():
                log.warning(f"Whisper produced empty stdout. stderr: {result.stderr[:500]}")
            # whisper.cpp outputs to stderr on some builds
            return result.stdout or result.stderr
        except subprocess.TimeoutExpired:
            log.error("Whisper timed out")
            return None
        except Exception as e:
            log.error(f"Whisper failed: {e}")
            return None

    def _parse_output(self, raw: str) -> list[TranscriptSegment]:
        """
        Parse whisper.cpp timestamped output.
        Format: [HH:MM:SS.mmm --> HH:MM:SS.mmm]   text
        """
        # Strip ANSI escape codes
        raw = re.sub(r"\x1b\[[0-9;]*m", "", raw)

        segments = []
        pattern = re.compile(
            r"\[(\d+):(\d+):(\d+)\.(\d+)\s*-->\s*\d+:\d+:\d+\.\d+\]\s*(.*)"
        )
        for line in raw.splitlines():
            m = pattern.match(line.strip())
            if m:
                h, min_, sec, ms_str, text = m.groups()
                start_ms = (
                    int(h) * 3600000
                    + int(min_) * 60000
                    + int(sec) * 1000
                    + int(ms_str[:3].ljust(3, "0"))
                )
                if text.strip():
                    segments.append(
                        TranscriptSegment(
                            start_ms=start_ms,
                            end_ms=start_ms,
                            text=text.strip(),
                        )
                    )

        # Fallback: if no timestamps parsed, grab any non-empty lines as plain text
        if not segments:
            log.warning("No timestamped segments found — falling back to raw text")
            for line in raw.splitlines():
                text = line.strip()
                # Skip whisper log lines
                if text and not text.startswith(("[", "whisper_", "main:", "system_info")):
                    segments.append(TranscriptSegment(start_ms=0, end_ms=0, text=text))

        return segments

    def _build_markdown(
        self,
        segments: list[TranscriptSegment],
        title: str,
        date: datetime,
    ) -> str:
        """Build the transcript file: a machine-readable YAML header, then the
        timestamped transcript. No prose. This file is a handoff for a
        downstream agent to enrich and summarize."""
        duration_s = max((seg.end_ms for seg in segments), default=0) / 1000.0

        lines = [build_frontmatter(title, date, duration_s, source="aloe-scribe"), ""]

        for seg in segments:
            text = (seg.text or "").strip()
            if text:
                lines.append(f"[{seg.timestamp()}] {text}")

        return "\n".join(lines) + "\n"
