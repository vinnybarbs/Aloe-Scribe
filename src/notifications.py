"""
notifications.py — Cross-platform desktop notifications for Aloe Scribe.

Uses notify-send on Linux and QSystemTrayIcon or osascript on macOS.
"""

import logging
import subprocess
import sys

log = logging.getLogger(__name__)

# Optional reference to a QSystemTrayIcon, set by ui_mac.py at startup
_tray_icon = None


def set_tray_icon(icon):
    """Register the QSystemTrayIcon so notifications route through it on macOS."""
    global _tray_icon
    _tray_icon = icon


def send(title: str, body: str):
    """Send a desktop notification on the current platform."""
    if sys.platform == "darwin":
        _send_macos(title, body)
    else:
        _send_linux(title, body)


def _send_linux(title: str, body: str):
    try:
        subprocess.Popen([
            "notify-send",
            "--icon=audio-input-microphone",
            title,
            body,
        ])
    except Exception as e:
        log.warning(f"notify-send failed: {e}")


def _send_macos(title: str, body: str):
    # Prefer QSystemTrayIcon if available (shows native notification)
    if _tray_icon is not None:
        try:
            from PyQt6.QtWidgets import QSystemTrayIcon
            _tray_icon.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, 5000)
            return
        except Exception:
            pass

    # Fallback to osascript
    try:
        subprocess.Popen([
            "osascript", "-e",
            f'display notification "{body}" with title "{title}"',
        ])
    except Exception as e:
        log.warning(f"osascript notification failed: {e}")
