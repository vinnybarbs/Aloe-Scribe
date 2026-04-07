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

        cmd = [
            self.binary_path,
            "-m", self.model_path,
            "-f", str(audio_path),
            "--output-txt",      # plain text output
            "-otxt",
            "--print-colors",    # speaker color hints (parsed below)
            "-l", "en",
        ]

        log.debug(f"Running: {' '.join(cmd)}")
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
            return result.stdout
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
                            end_ms=start_ms,  # end not critical for display
                            text=text.strip(),
                        )
                    )
        return segments

    def _build_markdown(
        self,
        segments: list[TranscriptSegment],
        title: str,
        date: datetime,
    ) -> str:
        """Build the final Markdown file."""
        date_str = date.strftime("%B %d, %Y")
        time_str = date.strftime("%I:%M %p")
        iso_date = date.strftime("%Y-%m-%d")

        lines = [
            f"# {title}",
            f"**Date:** {date_str}  ",
            f"**Time:** {time_str}  ",
            f"**Transcribed by:** Aloe Scribe  ",
            "",
            "---",
            "",
            "## Full Transcript",
            "",
        ]

        if not segments:
            lines.append("_No speech detected._")
        else:
            for seg in segments:
                lines.append(f"`{seg.timestamp()}` {seg.text}")
                lines.append("")

        lines += [
            "---",
            "",
            f"_Transcript generated {datetime.now().strftime('%Y-%m-%d %H:%M')} · Aloe Scribe_",
        ]

        return "\n".join(lines)
