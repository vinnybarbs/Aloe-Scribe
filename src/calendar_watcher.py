"""
calendar_watcher.py — Polls an iCal feed and fires callbacks when a meeting is approaching.

Works with Google Calendar, Outlook/M365, and any standard iCal URL.
No OAuth needed — just a read-only iCal URL from your calendar settings.
"""

import threading
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

import requests
from icalendar import Calendar, Event

log = logging.getLogger(__name__)


class Meeting:
    def __init__(self, title: str, start: datetime, end: datetime):
        self.title = title
        self.start = start
        self.end = end
        self.notified = False  # track so we don't notify twice

    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() / 60)

    def slug(self) -> str:
        """URL-safe slugified title for use in filenames."""
        import re
        s = self.title.lower()
        s = re.sub(r"[^\w\s-]", "", s)
        s = re.sub(r"[\s_]+", "-", s).strip("-")
        return s or "meeting"

    def __repr__(self):
        return f"<Meeting '{self.title}' at {self.start.strftime('%H:%M')}>"


class CalendarWatcher:
    """
    Polls an iCal URL on a background thread.
    Calls `on_upcoming(meeting)` when a meeting is `notify_minutes` away.
    Calls `on_started(meeting)` when a meeting's start time is reached.
    Calls `on_ended(meeting)` when a meeting's end time is reached.
    """

    def __init__(
        self,
        ical_url: str,
        notify_minutes: int = 4,
        poll_interval_minutes: int = 5,
        on_upcoming: Optional[Callable] = None,
        on_started: Optional[Callable] = None,
        on_ended: Optional[Callable] = None,
    ):
        self.ical_url = ical_url
        self.notify_minutes = notify_minutes
        self.poll_interval = poll_interval_minutes * 60
        self.on_upcoming = on_upcoming
        self.on_started = on_started
        self.on_ended = on_ended

        self._meetings: list[Meeting] = []
        self._active_meeting: Optional[Meeting] = None
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        log.info("Calendar watcher starting...")
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._refresh()
                self._check()
            except Exception as e:
                log.warning(f"Calendar error: {e}")
            self._stop_event.wait(self.poll_interval)

    def _refresh(self):
        """Fetch and parse the iCal feed."""
        log.debug("Fetching calendar...")
        resp = requests.get(self.ical_url, timeout=10)
        resp.raise_for_status()
        cal = Calendar.from_ical(resp.content)

        now = datetime.now(timezone.utc)
        window_end = now + timedelta(hours=12)  # look 12hr ahead
        meetings = []

        for component in cal.walk():
            if not isinstance(component, Event):
                continue
            title = str(component.get("SUMMARY", "Meeting"))
            dtstart = component.get("DTSTART")
            dtend = component.get("DTEND")
            if not dtstart or not dtend:
                continue

            start = self._to_utc(dtstart.dt)
            end = self._to_utc(dtend.dt)

            # Only care about upcoming meetings in the next 12 hours
            if start > now and start < window_end:
                meetings.append(Meeting(title=title, start=start, end=end))

        # Sort by start time
        self._meetings = sorted(meetings, key=lambda m: m.start)
        log.debug(f"Found {len(self._meetings)} upcoming meeting(s)")

    def _check(self):
        """Check if any meeting needs a notification or start/end signal."""
        now = datetime.now(timezone.utc)

        for meeting in self._meetings:
            minutes_until = (meeting.start - now).total_seconds() / 60

            # Notify window: between notify_minutes and notify_minutes-1
            if 0 < minutes_until <= self.notify_minutes and not meeting.notified:
                meeting.notified = True
                log.info(f"Upcoming: {meeting} in {minutes_until:.0f} min")
                if self.on_upcoming:
                    self.on_upcoming(meeting)

            # Meeting just started (within last 2 minutes)
            if -2 <= minutes_until <= 0 and self._active_meeting != meeting:
                self._active_meeting = meeting
                log.info(f"Started: {meeting}")
                if self.on_started:
                    self.on_started(meeting)

        # Check if active meeting ended
        if self._active_meeting:
            minutes_until_end = (
                self._active_meeting.end - now
            ).total_seconds() / 60
            if minutes_until_end < 0:
                log.info(f"Ended: {self._active_meeting}")
                if self.on_ended:
                    self.on_ended(self._active_meeting)
                self._active_meeting = None

    @staticmethod
    def _to_utc(dt) -> datetime:
        """Normalise date/datetime to UTC-aware datetime."""
        from datetime import date
        if isinstance(dt, date) and not isinstance(dt, datetime):
            # All-day event — treat as midnight UTC
            return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
