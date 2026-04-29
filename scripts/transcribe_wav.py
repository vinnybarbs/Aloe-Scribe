#!/usr/bin/env python3
"""
transcribe_wav.py — Post-call recovery tool.

Runs whisper.cpp on an existing WAV and writes the Markdown transcript
next to it. Use this when the app crashed mid-call and left behind a WAV,
or when a recording's ffmpeg process kept writing silence after the call.

Usage:
    python scripts/transcribe_wav.py ~/meetings/2026-04-17-1127-busy.wav
    python scripts/transcribe_wav.py <wav> --title "Busy call" --sync --delete-wav
"""

import argparse
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from transcriber import Transcriber
from syncer import Syncer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("transcribe-wav")


FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(\d{4})-(.+)$")


def infer_from_filename(wav: Path) -> tuple[str, datetime]:
    """Parse YYYY-MM-DD-HHMM-slug.wav → (title, datetime). Falls back to mtime/name."""
    m = FILENAME_RE.match(wav.stem)
    if m:
        date_str, time_str, slug = m.groups()
        try:
            dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H%M")
            title = slug.replace("-", " ").title()
            return title, dt
        except ValueError:
            pass
    return wav.stem.replace("-", " ").title(), datetime.fromtimestamp(wav.stat().st_mtime)


def main():
    p = argparse.ArgumentParser(description="Transcribe an orphan WAV via whisper.cpp")
    p.add_argument("wav", type=Path, help="Path to the .wav file")
    p.add_argument("--title", help="Meeting title (default: inferred from filename)")
    p.add_argument("--date", help="ISO datetime, e.g. 2026-04-17T11:27 (default: inferred)")
    p.add_argument("--output", type=Path, help="Output .md path (default: alongside WAV)")
    p.add_argument("--sync", action="store_true", help="Queue the .md for rclone sync")
    p.add_argument("--delete-wav", action="store_true", help="Delete WAV after success (default: keep)")
    args = p.parse_args()

    if not args.wav.exists():
        log.error(f"WAV not found: {args.wav}")
        sys.exit(1)

    config_path = ROOT / "config" / "config.toml"
    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    inferred_title, inferred_date = infer_from_filename(args.wav)
    title = args.title or inferred_title
    meeting_date = datetime.fromisoformat(args.date) if args.date else inferred_date

    md_path = args.output if args.output else args.wav.with_suffix(".md")

    log.info(f"Input:  {args.wav}")
    log.info(f"Output: {md_path}")
    log.info(f"Title:  {title}")
    log.info(f"Date:   {meeting_date.isoformat()}")

    transcriber = Transcriber(
        binary_path=config["whisper"]["binary_path"],
        model_path=config["whisper"]["model_path"],
    )

    result = transcriber.transcribe(
        audio_path=args.wav,
        output_path=md_path,
        meeting_title=title,
        meeting_date=meeting_date,
    )

    if not result:
        log.error("Transcription failed")
        sys.exit(1)

    log.info(f"Transcript written: {md_path}")

    if args.sync and config["sync"]["enabled"]:
        syncer = Syncer(
            rclone_remote=config["sync"]["rclone_remote"],
            enabled=True,
            notify_on_sync=config["app"]["notify_on_sync"],
        )
        syncer.start()
        syncer.enqueue(md_path)
        import time
        time.sleep(90)
        syncer.stop()

    if args.delete_wav:
        args.wav.unlink()
        log.info(f"Deleted WAV: {args.wav}")
    else:
        log.info(f"Kept WAV: {args.wav}")


if __name__ == "__main__":
    main()
