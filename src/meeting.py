"""
meeting.py — Minimal Meeting dataclass used as a lightweight recording label.

Formerly lived inside calendar_watcher.py. Kept as a thin structure so the
rest of the app can pass a "what are we recording" object without needing
start/end/calendar semantics.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Meeting:
    title: str
    start: datetime = field(default_factory=lambda: datetime.now().astimezone())
    end: datetime = field(default_factory=lambda: datetime.now().astimezone())

    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() / 60)

    def slug(self) -> str:
        """URL-safe slugified title for use in filenames."""
        s = self.title.lower()
        s = re.sub(r"[^\w\s-]", "", s)
        s = re.sub(r"[\s_]+", "-", s).strip("-")
        return s or "meeting"

    def __repr__(self):
        return f"<Meeting '{self.title}' at {self.start.strftime('%H:%M')}>"
