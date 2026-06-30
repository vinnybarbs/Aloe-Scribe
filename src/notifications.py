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
    elif sys.platform == "win32":
        _send_windows(title, body)
    else:
        _send_linux(title, body)


def _send_windows(title: str, body: str):
    # Route through the Qt system tray balloon. The Windows UI registers its
    # QSystemTrayIcon via set_tray_icon() at startup, so this picks up the
    # Aloe Scribe icon and needs no extra dependency.
    if _tray_icon is not None:
        try:
            _tray_icon.showMessage(title, body)
            return
        except Exception as e:
            log.warning(f"tray notification failed: {e}")
    log.info(f"notification: {title} - {body}")


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
    # Use NSUserNotification via PyObjC so the notification picks up the
    # calling app's bundle icon (the Aloe Scribe leaf). osascript's
    # `display notification` is attributed to Script Editor and shows a
    # generic scroll icon — not what we want.
    try:
        from Foundation import NSUserNotification, NSUserNotificationCenter
        notification = NSUserNotification.alloc().init()
        notification.setTitle_(title)
        notification.setInformativeText_(body)
        center = NSUserNotificationCenter.defaultUserNotificationCenter()
        center.deliverNotification_(notification)
        return
    except Exception as e:
        log.warning(f"NSUserNotification failed: {e}")

    # Fallback: osascript (will show generic icon, but at least notifies).
    try:
        subprocess.Popen([
            "osascript", "-e",
            f'display notification "{body}" with title "{title}"',
        ])
    except Exception as e:
        log.warning(f"osascript notification failed: {e}")
