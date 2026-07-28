"""
frontmatter.py — YAML frontmatter for transcripts.

The transcript is a handoff file: the user feeds it to whatever work AI they
have (Copilot, Glean, etc.) to enrich with calendar/email/chat and produce a
summary doc. This block gives that downstream agent the one thing it can't
recover from the transcript text alone — *which meeting this was*, as a time
window — so it can match the calendar event (and from there, attendees and
related threads). No participants are listed here on purpose: the agent infers
them from the matched calendar event.

Identical output to the iOS writer (ios/AloeScribe/Storage/TranscriptWriter.swift)
so phone and desktop transcripts carry the same metadata.
"""

from datetime import datetime, timedelta


def build_frontmatter(
    title: str,
    start: datetime,
    duration_seconds: float,
    source: str = "aloe-scribe",
    extras: list = None,
) -> str:
    """Return a YAML frontmatter block (including the `---` fences, no trailing
    newline). `start` may be naive (assumed local) or tz-aware; both are
    emitted as ISO-8601 with an explicit offset.

    `extras`: optional pre-formatted YAML lines appended before the closing
    fence (used for the speaker-label key when the recording was split).
    The iOS writer does not emit these, which is fine — they are additive."""
    start_local = start.astimezone()
    end_local = (start + timedelta(seconds=max(0.0, duration_seconds))).astimezone()
    duration_min = max(0, round(duration_seconds / 60))

    # Quote the title so colons / special chars stay valid YAML.
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')

    lines = [
        "---",
        f'title: "{safe_title}"',
        f"date: {start_local.isoformat(timespec='seconds')}",
        f"end: {end_local.isoformat(timespec='seconds')}",
        f"duration_min: {duration_min}",
        f"source: {source}",
    ]
    lines.extend(extras or [])
    lines.append("---")
    return "\n".join(lines)
