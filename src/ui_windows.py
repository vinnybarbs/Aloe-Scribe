"""
ui_windows.py — Windows UI for Aloe Scribe.

Reuses the PyQt6 window and app from ui_mac.py (the design, layout, live
preview, and all the recording controls) and overrides only the two pieces
that are Apple-specific:

  1. The system tray. ui_mac bypasses Qt's QSystemTrayIcon and uses NSStatusItem
     via PyObjC because Qt's tray fails to draw on recent macOS. On Windows
     QSystemTrayIcon works, so we use it directly.

  2. The audio meters. ui_mac drives them from avfoundation / ScreenCaptureKit.
     Live WASAPI metering is a later enhancement; for now the meter is a no-op
     so the window runs without the Mac capture path (the level bars stay flat).

Everything else is inherited, so a change to the shared UI lands on both
platforms with no duplication. The Mac runtime never imports this file.
"""

import logging
import os
import sys
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon, QMenu

from ui_mac import AloeScribeWindow, AloeScribeApp, _make_leaf_icon

log = logging.getLogger(__name__)


def _icon_path() -> Path:
    if getattr(sys, "frozen", False):
        # PyInstaller unpacks data next to the executable / in _MEIPASS.
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return base / "assets" / "icon.png"
    return Path(__file__).parent.parent / "assets" / "icon.png"


class WindowsAloeScribeWindow(AloeScribeWindow):
    """Mac window with the avfoundation/SCK meters and Finder calls swapped
    for Windows-safe behavior."""

    def _start_meters(self):
        # WASAPI live metering is a v2 item. No-op so the window runs without
        # the Mac meter path; the bars simply stay flat.
        return

    def _stop_meters(self):
        return

    def _open_folder(self):
        path = (
            Path(self._output_dir).expanduser()
            if self._output_dir
            else Path("~/meetings").expanduser()
        )
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning(f"Could not create {path}: {e}")
            path = Path("~/meetings").expanduser()
            path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(path))  # noqa: triggers Explorer on Windows
        except Exception as e:
            log.warning(f"Could not open folder {path}: {e}")


class AloeScribeApp(AloeScribeApp):  # noqa: F811 — intentional Windows override
    """Windows app shell: real Qt system tray instead of NSStatusItem, no macOS
    dock setup, and a Windows-safe window subclass."""

    def run(self):
        self._app = QApplication(sys.argv)
        self._app.setApplicationName("Aloe Scribe")
        self._app.setApplicationDisplayName("Aloe Scribe")

        icon_path = _icon_path()
        if icon_path.exists():
            self._app.setWindowIcon(QIcon(str(icon_path)))
        else:
            self._app.setWindowIcon(_make_leaf_icon())

        # Stay alive in the tray when the window is closed.
        self._app.setQuitOnLastWindowClosed(False)

        # Mirror ui_mac.run()'s full callback set — the port originally
        # passed only the early callbacks, which silently dropped meeting
        # metadata, transcript saves, merges, speaker naming, and the
        # resummarize hook on Windows.
        self._window = WindowsAloeScribeWindow(
            on_start_recording=self._on_start_recording,
            on_stop_recording=self._on_stop_recording,
            on_quit=self._on_quit,
            list_sources=self._list_sources,
            on_device_change=self._on_device_change,
            on_output_dir_change=self._on_output_dir_change,
            on_transcribe_file=self._on_transcribe_file,
            on_name_speakers=self._on_name_speakers,
            on_meta_changed=self._on_meta_changed,
            on_save_transcript=self._on_save_transcript,
            on_merge_transcripts=self._on_merge_transcripts,
            on_resummarize=self._on_resummarize,
            live_preview=self._live_preview,
            current_mic=self._current_mic,
            current_system=self._current_system,
            current_output_dir=self._current_output_dir,
        )

        self._setup_tray()
        self._window._tray = self._tray
        self._window._app_ref = self

        # Route notifications through the Qt tray balloon.
        import notifications
        notifications.set_tray_icon(self._tray)

        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

        sys.exit(self._app.exec())

    def _setup_tray(self):
        icon_path = _icon_path()
        icon = QIcon(str(icon_path)) if icon_path.exists() else _make_leaf_icon()
        tray = QSystemTrayIcon(icon)
        tray.setToolTip("Aloe Scribe")

        menu = QMenu()
        menu.addAction("Show window", self._show_window)
        menu.addAction("Open transcripts folder", self._open_folder)
        menu.addSeparator()
        menu.addAction("Quit Aloe Scribe", self._quit)
        tray.setContextMenu(menu)

        # Left-click toggles the window; the menu is the right-click context.
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._tray = tray

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_window()

    def _update_tray_menu(self):
        if self._tray is None:
            return
        status_map = {
            "idle": "Aloe Scribe",
            "recording": "Aloe Scribe - Recording",
            "processing": "Aloe Scribe - Transcribing",
            "done": "Aloe Scribe - Transcript saved",
        }
        state = self._window._state if self._window else "idle"
        try:
            self._tray.setToolTip(status_map.get(state, "Aloe Scribe"))
        except Exception as e:
            log.warning(f"tray update failed: {e}")

    def _open_folder(self):
        if self._window is not None:
            self._window._open_folder()
            return
        path = (
            Path(self._current_output_dir).expanduser()
            if self._current_output_dir
            else Path("~/meetings").expanduser()
        )
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(path))  # noqa
        except Exception as e:
            log.warning(f"Could not open folder {path}: {e}")

    def _quit(self):
        if self._window and self._window._state == "recording":
            log.info("Quit requested during recording - stopping + transcribing first")
            self._window._quit_after_transcribe = True
            self._window._on_stop()
            return
        self._on_quit()
        if self._tray is not None:
            try:
                self._tray.hide()
            except Exception:
                pass
        if self._app:
            self._app.quit()
