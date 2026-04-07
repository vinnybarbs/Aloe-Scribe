"""
tray.py — Linux system tray app for Aloe Scribe using pystray.

Shows status in the tray icon, handles the Start/Skip notification flow,
and provides a right-click menu for manual control.
"""

import logging
import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

import pystray
from PIL import Image, ImageDraw

log = logging.getLogger(__name__)


def _make_icon(color: str = "#3A8C5A") -> Image.Image:
    """
    Draw the Aloe Scribe leaf tray icon at 64x64.
    color: idle=#3A8C5A  recording=#C94040  processing=#3878C8
    """
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Leaf shape (simplified polygon)
    leaf = [
        (32, 4),
        (52, 18),
        (56, 36),
        (46, 52),
        (32, 60),
        (18, 52),
        (8, 36),
        (12, 18),
    ]
    d.polygon(leaf, fill=color)

    # Stem line
    d.line([(32, 60), (32, 44)], fill="white", width=3)
    # Vein lines
    d.line([(32, 44), (22, 36)], fill="white", width=2)
    d.line([(32, 44), (42, 36)], fill="white", width=2)

    return img


def _send_notification(title: str, body: str):
    """Send a desktop notification via notify-send."""
    try:
        subprocess.Popen(["notify-send", "--icon=audio-input-microphone", title, body])
    except Exception as e:
        log.warning(f"notify-send failed: {e}")


class AloeScribeTray:
    """
    System tray icon + menu.

    States: idle → notifying → recording → processing → done → idle
    """

    def __init__(
        self,
        on_start_recording: Callable,
        on_stop_recording: Callable,
        on_quit: Callable,
    ):
        self.on_start_recording = on_start_recording
        self.on_stop_recording = on_stop_recording
        self.on_quit = on_quit

        self._state = "idle"
        self._current_meeting: Optional[object] = None
        self._icon: Optional[pystray.Icon] = None
        self._build_icon()

    # ------------------------------------------------------------------ #
    # Public state transitions                                             #
    # ------------------------------------------------------------------ #

    def notify_upcoming(self, meeting):
        """Called when a meeting is approaching — show Start/Skip prompt."""
        self._current_meeting = meeting
        self._set_state("notifying")

        # Desktop notification with action buttons (requires libnotify ≥0.7.9)
        # Falls back gracefully if actions aren't supported
        _send_notification(
            "Aloe Scribe",
            f"Meeting in ~{4} min: {meeting.title}\nOpen Aloe Scribe to start recording.",
        )
        self._update_menu()
        log.info(f"Notified: {meeting.title}")

    def set_recording(self, meeting):
        """Switch to recording state."""
        self._current_meeting = meeting
        self._set_state("recording")
        self._update_menu()

    def set_processing(self):
        """Switch to processing/transcribing state."""
        self._set_state("processing")
        self._update_menu()

    def set_done(self, output_path: Path):
        """Switch to done state and notify."""
        self._set_state("done")
        self._update_menu()
        _send_notification(
            "Aloe Scribe — Done",
            f"Transcript saved: {output_path.name}",
        )
        # Auto-return to idle after 10 seconds
        threading.Timer(10, self.set_idle).start()

    def set_idle(self):
        self._current_meeting = None
        self._set_state("idle")
        self._update_menu()

    # ------------------------------------------------------------------ #
    # Tray icon lifecycle                                                  #
    # ------------------------------------------------------------------ #

    def run(self):
        """Block and run the tray icon. Call from main thread."""
        self._icon.run()

    def stop(self):
        if self._icon:
            self._icon.stop()

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    _STATE_COLORS = {
        "idle": "#3A8C5A",
        "notifying": "#E5A020",
        "recording": "#C94040",
        "processing": "#3878C8",
        "done": "#3A8C5A",
    }

    def _set_state(self, state: str):
        self._state = state
        if self._icon:
            self._icon.icon = _make_icon(self._STATE_COLORS.get(state, "#888888"))

    def _build_icon(self):
        self._icon = pystray.Icon(
            name="aloe-scribe",
            icon=_make_icon(),
            title="Aloe Scribe",
            menu=self._build_menu(),
        )

    def _build_menu(self) -> pystray.Menu:
        state = self._state
        meeting = self._current_meeting
        meeting_label = meeting.title if meeting else "No meeting"

        items = []

        # Status header (non-clickable)
        status_text = {
            "idle": "Aloe Scribe — idle",
            "notifying": f"Meeting soon: {meeting_label}",
            "recording": f"Recording: {meeting_label}",
            "processing": "Transcribing...",
            "done": "Done — transcript saved",
        }.get(state, "Aloe Scribe")

        items.append(pystray.MenuItem(status_text, None, enabled=False))
        items.append(pystray.Menu.SEPARATOR)

        if state == "notifying":
            items.append(pystray.MenuItem(
                "Start recording",
                lambda icon, item: self._handle_start()
            ))
            items.append(pystray.MenuItem(
                "Skip this meeting",
                lambda icon, item: self.set_idle()
            ))

        elif state == "recording":
            items.append(pystray.MenuItem(
                "Stop & transcribe",
                lambda icon, item: self._handle_stop()
            ))

        elif state == "idle":
            items.append(pystray.MenuItem(
                "Start recording now",
                lambda icon, item: self._handle_manual_start()
            ))

        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem(
            "Open meetings folder",
            lambda icon, item: self._open_meetings_folder()
        ))
        items.append(pystray.MenuItem(
            "Quit Aloe Scribe",
            lambda icon, item: self._handle_quit()
        ))

        return pystray.Menu(*items)

    def _update_menu(self):
        if self._icon:
            self._icon.menu = self._build_menu()

    def _handle_start(self):
        if self._current_meeting:
            self.set_recording(self._current_meeting)
            self.on_start_recording(self._current_meeting)

    def _handle_manual_start(self):
        """Start recording without a calendar event."""
        from calendar_watcher import Meeting
        manual = Meeting(
            title="Manual Recording",
            start=datetime.now().astimezone(),
            end=datetime.now().astimezone(),
        )
        self._current_meeting = manual
        self.set_recording(manual)
        self.on_start_recording(manual)

    def _handle_stop(self):
        self.set_processing()
        self.on_stop_recording()

    def _handle_quit(self):
        self.stop()
        self.on_quit()

    def _open_meetings_folder(self):
        meetings_dir = Path("~/meetings").expanduser()
        meetings_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(["xdg-open", str(meetings_dir)])
        except Exception as e:
            log.warning(f"Could not open folder: {e}")
