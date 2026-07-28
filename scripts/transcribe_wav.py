#!/usr/bin/env python3
"""
transcribe_wav.py — Post-call recovery tool.

Transcribes WAV files that the app left behind (crash mid-call, or a
transcription backend that failed/raised and so never cleaned up the WAV).
Writes the Markdown transcript next to each WAV.

By default it uses the SAME backend the app is configured for in
config/config.toml (Parakeet), so recovered transcripts match your normal
ones. If Parakeet fails and whisper.cpp is installed, it automatically falls
back to whisper unless --no-fallback is given.

Usage:
    # One file:
    python scripts/transcribe_wav.py ~/meetings/2026-04-17-1127-busy.wav

    # Every orphan WAV in your meetings folder (no .md sibling yet):
    python scripts/transcribe_wav.py

    # A specific folder, forcing whisper, and syncing the results:
    python scripts/transcribe_wav.py ~/meetings --backend whisper --sync

Options:
    --title       Override inferred meeting title (single-file only)
    --date        ISO datetime, e.g. 2026-04-17T11:27 (single-file only)
    --output      Output .md path (single-file only; default: alongside WAV)
    --backend     Force "parakeet" or "whisper" (default: from config)
    --no-fallback Don't fall back to whisper if the primary backend fails
    --sync        Queue the .md(s) for rclone sync
    --delete-wav  Delete each WAV after a successful transcription
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


def build_transcriber(config: dict, backend: str):
    """Return a transcriber instance for the named backend ("parakeet"/"whisper")."""
    if backend == "parakeet":
        from transcriber_parakeet import ParakeetTranscriber

        model_id = config.get("transcriber", {}).get(
            "parakeet_model", ParakeetTranscriber.DEFAULT_MODEL
        )
        return ParakeetTranscriber(model=model_id)
    return Transcriber(
        binary_path=config["whisper"]["binary_path"],
        model_path=config["whisper"]["model_path"],
    )


def whisper_available(config: dict) -> bool:
    try:
        return (
            Path(config["whisper"]["binary_path"]).exists()
            and Path(config["whisper"]["model_path"]).exists()
        )
    except Exception:
        return False


def transcribe_one(
    wav: Path,
    config: dict,
    primary_backend: str,
    allow_fallback: bool,
    title: str | None = None,
    date: str | None = None,
    output: Path | None = None,
) -> Path | None:
    """Transcribe a single WAV, with optional whisper fallback. Returns md path or None."""
    inferred_title, inferred_date = infer_from_filename(wav)
    meeting_title = title or inferred_title
    meeting_date = datetime.fromisoformat(date) if date else inferred_date
    md_path = output if output else wav.with_suffix(".md")

    log.info(f"Input:  {wav}")
    log.info(f"Output: {md_path}")
    log.info(f"Title:  {meeting_title}  ({meeting_date.isoformat()})")

    # Split recordings are stereo (ch0 = mic, ch1 = system). The STT backends
    # expect mono, so downmix to a hidden temp file — and when the backend can
    # emit timestamped sentences, produce the speaker-labeled transcript the
    # app itself would have written.
    import speakers

    stt_input = wav
    tmp_mono = None
    if speakers.wav_channels(wav) == 2:
        tmp = wav.with_name(f".{wav.stem}.mono.wav")
        if speakers.downmix_to_mono_wav(wav, tmp):
            stt_input, tmp_mono = tmp, tmp
            log.info("Split (stereo) recording — downmixed for STT, labeling speakers")

    # Try primary backend, then whisper fallback if allowed and available.
    backends = [primary_backend]
    if (
        allow_fallback
        and primary_backend != "whisper"
        and whisper_available(config)
    ):
        backends.append("whisper")

    try:
        for backend in backends:
            log.info(f"Transcribing with backend: {backend}")
            result = None
            try:
                transcriber = build_transcriber(config, backend)
                if tmp_mono is not None and hasattr(transcriber, "transcribe_sentences"):
                    sentences = transcriber.transcribe_sentences(stt_input)
                    res = (
                        speakers.label_sentences(wav, sentences, speakers.Diarizer())
                        if sentences
                        else None
                    )
                    if res:
                        labeled, diarized = res
                        from frontmatter import build_frontmatter

                        source = (
                            "aloe-scribe-windows"
                            if sys.platform == "win32"
                            else "aloe-scribe-mac"
                        )
                        duration = max((s.end for s in labeled), default=0.0)
                        fm = build_frontmatter(
                            meeting_title,
                            meeting_date,
                            duration,
                            source=source,
                            extras=speakers.speaker_frontmatter_extras(labeled, diarized),
                        )
                        md_path.write_text(
                            fm + "\n\n" + speakers.build_labeled_body(labeled) + "\n",
                            encoding="utf-8",
                        )
                        result = md_path
                if result is None:
                    result = transcriber.transcribe(
                        audio_path=stt_input,
                        output_path=md_path,
                        meeting_title=meeting_title,
                        meeting_date=meeting_date,
                    )
            except Exception as e:
                log.error(f"  {backend} raised: {e}")
                result = None
            if result:
                log.info(f"Transcript written ({backend}): {md_path}")
                return md_path
            log.warning(f"  {backend} produced no transcript")
    finally:
        if tmp_mono is not None:
            try:
                tmp_mono.unlink()
            except Exception:
                pass

    log.error(f"All backends failed for: {wav}")
    return None


def find_orphan_wavs(directory: Path) -> list[Path]:
    """WAV files in directory with no matching .md sibling, oldest first."""
    orphans = [w for w in sorted(directory.glob("*.wav")) if not w.with_suffix(".md").exists()]
    return orphans


def main():
    p = argparse.ArgumentParser(description="Transcribe orphan WAV file(s)")
    p.add_argument(
        "wav",
        type=Path,
        nargs="?",
        help="Path to a .wav file or a folder. Omit to scan the configured meetings folder.",
    )
    p.add_argument("--title", help="Meeting title (single-file only; default: inferred)")
    p.add_argument("--date", help="ISO datetime (single-file only; default: inferred)")
    p.add_argument("--output", type=Path, help="Output .md path (single-file only)")
    p.add_argument(
        "--backend",
        choices=["parakeet", "whisper"],
        help="Force a backend (default: from config.toml)",
    )
    p.add_argument(
        "--no-fallback",
        action="store_true",
        help="Don't fall back to whisper if the primary backend fails",
    )
    p.add_argument("--sync", action="store_true", help="Queue the .md(s) for rclone sync")
    p.add_argument("--delete-wav", action="store_true", help="Delete each WAV after success")
    args = p.parse_args()

    config_path = ROOT / "config" / "config.toml"
    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    primary_backend = (
        args.backend or config.get("transcriber", {}).get("backend", "whisper")
    ).lower()
    allow_fallback = not args.no_fallback

    # Resolve the work list: a single file, a directory, or the configured dir.
    if args.wav and args.wav.is_file():
        wavs = [args.wav]
    elif args.wav and args.wav.is_dir():
        wavs = find_orphan_wavs(args.wav)
    elif args.wav:
        log.error(f"Not found: {args.wav}")
        sys.exit(1)
    else:
        local_dir = Path(config["output"]["local_dir"]).expanduser()
        wavs = find_orphan_wavs(local_dir)
        log.info(f"Scanning {local_dir} for orphan WAVs…")

    if not wavs:
        log.info("No WAV files to transcribe. Nothing to do.")
        return

    if len(wavs) > 1 and (args.title or args.date or args.output):
        log.error("--title/--date/--output only apply to a single WAV file.")
        sys.exit(1)

    log.info(f"{len(wavs)} WAV file(s) to process. Primary backend: {primary_backend}")

    syncer = None
    if args.sync and config["sync"]["enabled"]:
        syncer = Syncer(
            rclone_remote=config["sync"]["rclone_remote"],
            enabled=True,
            notify_on_sync=config["app"]["notify_on_sync"],
        )
        syncer.start()

    succeeded, failed = [], []
    for wav in wavs:
        md_path = transcribe_one(
            wav,
            config,
            primary_backend,
            allow_fallback,
            title=args.title,
            date=args.date,
            output=args.output,
        )
        if md_path:
            succeeded.append(wav)
            if syncer:
                syncer.enqueue(md_path)
            if args.delete_wav:
                wav.unlink()
                log.info(f"Deleted WAV: {wav}")
        else:
            failed.append(wav)

    if syncer:
        import time

        log.info("Waiting for sync to flush…")
        time.sleep(90)
        syncer.stop()

    log.info(f"Done. {len(succeeded)} succeeded, {len(failed)} failed.")
    if failed:
        for w in failed:
            log.error(f"  FAILED: {w}")
        sys.exit(1)


if __name__ == "__main__":
    main()
