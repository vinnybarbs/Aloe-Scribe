"""
ui_mac.py — Aloe Scribe macOS UI using PyQt6.

Provides a floating window, dock icon, and system tray icon (menu bar).
Single-instance: re-launching activates the existing window.
"""

import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

from PyQt6.QtCore import Qt, QTimer, QPoint, pyqtSignal, QObject
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QPolygon, QPen, QAction
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSystemTrayIcon,
    QMenu,
    QFrame,
)

def _setup_macos_app():
    """Register as a proper macOS GUI app with dock icon."""
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyRegular
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        app.activateIgnoringOtherApps_(True)
    except ImportError:
        log.warning("PyObjC not installed — dock icon may not appear")

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Leaf icon generator using QPainter (no Pillow dependency)
# ---------------------------------------------------------------------------
_LEAF_POINTS = [
    (32, 4), (52, 18), (56, 36), (46, 52),
    (32, 60), (18, 52), (8, 36), (12, 18),
]

_STATE_COLORS = {
    "idle":       "#3A8C5A",
    "notifying":  "#E5A020",
    "recording":  "#C94040",
    "processing": "#3878C8",
    "done":       "#3A8C5A",
}


def _make_leaf_pixmap(color: str = "#3A8C5A", size: int = 64) -> QPixmap:
    """Draw the aloe leaf icon as a QPixmap."""
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))

    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Leaf polygon
    polygon = QPolygon([QPoint(int(x), int(y)) for x, y in _LEAF_POINTS])

    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawPolygon(polygon)

    # Stem + veins
    pen = QPen(QColor("white"))
    pen.setWidth(3)
    p.setPen(pen)
    p.drawLine(32, 60, 32, 44)

    pen.setWidth(2)
    p.setPen(pen)
    p.drawLine(32, 44, 22, 36)
    p.drawLine(32, 44, 42, 36)

    p.end()
    return pixmap


def _make_leaf_icon(color: str = "#3A8C5A") -> QIcon:
    return QIcon(_make_leaf_pixmap(color))


# ---------------------------------------------------------------------------
# Stylesheet (mirrors the GTK CSS)
# ---------------------------------------------------------------------------
_STYLESHEET = """
    QMainWindow, QWidget#central {
        background-color: #ffffff;
    }
    QLabel#appTitle {
        font-size: 13px;
        font-weight: bold;
        letter-spacing: 2px;
        color: #3A8C5A;
    }
    QLabel#appSub {
        font-size: 11px;
        color: #3A8C5A;
        letter-spacing: 1px;
    }
    QLabel#stateLabel {
        font-size: 10px;
        color: #999999;
        letter-spacing: 2px;
    }
    QLabel#meetingTitle {
        font-size: 14px;
        font-weight: bold;
        color: #222222;
    }
    QLabel#meetingTime {
        font-size: 11px;
        color: #888888;
    }
    QLabel#timer {
        font-size: 32px;
        font-weight: bold;
        color: #222222;
        font-family: "Menlo", "SF Mono", "Courier New", monospace;
    }
    QLabel#statusIdle    { color: #888888; }
    QLabel#statusRecord  { color: #C94040; }
    QLabel#statusProcess { color: #3878C8; }
    QLabel#statusDone    { color: #3A8C5A; }
    QPushButton#btnStart {
        background-color: #3A8C5A;
        color: white;
        border-radius: 6px;
        font-weight: bold;
        font-size: 13px;
        padding: 8px 16px;
        border: none;
    }
    QPushButton#btnStart:hover { background-color: #2E7048; }
    QPushButton#btnStop {
        background-color: #C94040;
        color: white;
        border-radius: 6px;
        font-weight: bold;
        font-size: 13px;
        padding: 8px 16px;
        border: none;
    }
    QPushButton#btnStop:hover { background-color: #A83535; }
    QPushButton#btnSkip {
        background-color: #f0f0f0;
        color: #555555;
        border-radius: 6px;
        font-size: 13px;
        padding: 8px 16px;
        border: none;
    }
    QPushButton#btnSkip:hover { background-color: #e0e0e0; }
    QFrame#separator {
        background-color: #eeeeee;
        max-height: 1px;
    }
"""


# ---------------------------------------------------------------------------
# Signals bridge — thread-safe state updates from background threads
# ---------------------------------------------------------------------------
class _Signals(QObject):
    notify_upcoming = pyqtSignal(object)
    set_recording = pyqtSignal(object)
    set_processing = pyqtSignal()
    set_done = pyqtSignal(object)
    set_idle = pyqtSignal()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class AloeScribeWindow(QMainWindow):
    def __init__(
        self,
        on_start_recording: Callable,
        on_stop_recording: Callable,
        on_quit: Callable,
    ):
        super().__init__()
        self.on_start_recording = on_start_recording
        self.on_stop_recording = on_stop_recording
        self.on_quit = on_quit

        self._current_meeting = None
        self._state = "idle"
        self._timer_seconds = 0

        # Signals for thread-safe updates
        self._signals = _Signals()
        self._signals.notify_upcoming.connect(self._render_notify)
        self._signals.set_recording.connect(self._render_recording)
        self._signals.set_processing.connect(self._render_processing)
        self._signals.set_done.connect(self._render_done)
        self._signals.set_idle.connect(self._render_idle)

        # Timer
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick_timer)

        # Window setup
        self.setWindowTitle("Aloe Scribe")
        self.setFixedSize(300, 200)
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setStyleSheet(_STYLESHEET)

        # Central widget
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)

        self._main_layout = QVBoxLayout(central)
        self._main_layout.setContentsMargins(20, 20, 20, 20)
        self._main_layout.setSpacing(8)

        self._build_header()
        self._add_separator()

        # Content area
        self._content_widget = QWidget()
        self._content_layout = QVBoxLayout(self._content_widget)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(8)
        self._main_layout.addWidget(self._content_widget)

        self._render_idle()

    def _build_header(self):
        header = QHBoxLayout()

        title = QLabel("ALOE")
        title.setObjectName("appTitle")
        sub = QLabel("SCRIBE")
        sub.setObjectName("appSub")

        header.addWidget(title)
        header.addWidget(sub)
        header.addStretch()

        self._status_label = QLabel("● IDLE")
        self._status_label.setObjectName("statusIdle")
        header.addWidget(self._status_label)

        self._main_layout.addLayout(header)

    def _add_separator(self):
        sep = QFrame()
        sep.setObjectName("separator")
        sep.setFrameShape(QFrame.Shape.HLine)
        self._main_layout.addWidget(sep)

    def _clear_content(self):
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                # Clear nested layout
                while item.layout().count():
                    child = item.layout().takeAt(0)
                    if child.widget():
                        child.widget().deleteLater()

    # ------------------------------------------------------------------ #
    # State renderers                                                       #
    # ------------------------------------------------------------------ #

    def _render_idle(self):
        self._state = "idle"
        self._timer.stop()
        self._clear_content()

        label = QLabel("No meetings detected.")
        label.setObjectName("meetingTime")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(label)

        sub = QLabel("Watching your calendar...")
        sub.setObjectName("stateLabel")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(sub)

        btn = QPushButton("Start Recording Now")
        btn.setObjectName("btnStart")
        btn.clicked.connect(self._on_manual_start)
        self._content_layout.addWidget(btn)

        self._update_status("● IDLE", "statusIdle")

    def _render_notify(self, meeting):
        self._state = "notifying"
        self._current_meeting = meeting
        self._clear_content()

        state = QLabel("MEETING SOON")
        state.setObjectName("stateLabel")
        state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(state)

        title = QLabel(meeting.title)
        title.setObjectName("meetingTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(title)

        time_str = meeting.start.strftime("%I:%M %p")
        time_label = QLabel(f"Starts in ~4 minutes \u00b7 {time_str}")
        time_label.setObjectName("meetingTime")
        time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(time_label)

        btn_row = QHBoxLayout()
        skip = QPushButton("Skip")
        skip.setObjectName("btnSkip")
        skip.clicked.connect(lambda: self._signals.set_idle.emit())

        start = QPushButton("Start Recording")
        start.setObjectName("btnStart")
        start.clicked.connect(lambda: self._on_start(meeting))

        btn_row.addWidget(skip)
        btn_row.addWidget(start)
        self._content_layout.addLayout(btn_row)

        self._update_status("● MEETING SOON", "statusIdle")

    def _render_recording(self, meeting):
        self._state = "recording"
        self._current_meeting = meeting
        self._clear_content()

        state = QLabel("RECORDING")
        state.setObjectName("stateLabel")
        state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(state)

        title = QLabel(meeting.title if meeting else "Recording")
        title.setObjectName("meetingTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(title)

        self._timer_label = QLabel("00:00")
        self._timer_label.setObjectName("timer")
        self._timer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(self._timer_label)

        stop = QPushButton("Stop & Transcribe")
        stop.setObjectName("btnStop")
        stop.clicked.connect(self._on_stop)
        self._content_layout.addWidget(stop)

        self._update_status("● RECORDING", "statusRecord")
        self._timer_seconds = 0
        self._timer.start()

    def _render_processing(self):
        self._state = "processing"
        self._timer.stop()
        self._clear_content()

        state = QLabel("TRANSCRIBING")
        state.setObjectName("stateLabel")
        state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(state)

        label = QLabel("Running Whisper locally...")
        label.setObjectName("meetingTime")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(label)

        # Simple animated dots via timer
        self._dots_label = QLabel("...")
        self._dots_label.setObjectName("stateLabel")
        self._dots_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(self._dots_label)

        self._update_status("● PROCESSING", "statusProcess")

    def _render_done(self, output_path):
        self._state = "done"
        self._clear_content()

        state = QLabel("COMPLETE")
        state.setObjectName("stateLabel")
        state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(state)

        fname = QLabel(output_path.name if hasattr(output_path, 'name') else str(output_path))
        fname.setObjectName("meetingTime")
        fname.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(fname)

        btn_row = QHBoxLayout()
        done_btn = QPushButton("Done")
        done_btn.setObjectName("btnSkip")
        done_btn.clicked.connect(lambda: self._signals.set_idle.emit())

        open_btn = QPushButton("Open Folder")
        open_btn.setObjectName("btnStart")
        open_btn.clicked.connect(self._open_folder)

        btn_row.addWidget(done_btn)
        btn_row.addWidget(open_btn)
        self._content_layout.addLayout(btn_row)

        self._update_status("● DONE", "statusDone")

        # Auto-return to idle after 10s
        QTimer.singleShot(10000, self._signals.set_idle.emit)

    # ------------------------------------------------------------------ #
    # Public state setters (thread-safe via signals)                       #
    # ------------------------------------------------------------------ #

    def notify_upcoming(self, meeting):
        self._signals.notify_upcoming.emit(meeting)

    def set_recording(self, meeting):
        self._signals.set_recording.emit(meeting)

    def set_processing(self):
        self._signals.set_processing.emit()

    def set_done(self, output_path):
        self._signals.set_done.emit(output_path)

    def set_idle(self):
        self._signals.set_idle.emit()

    # ------------------------------------------------------------------ #
    # Handlers                                                             #
    # ------------------------------------------------------------------ #

    def _on_start(self, meeting):
        log.info(f"UI: starting recording for '{meeting.title}'")
        self._signals.set_recording.emit(meeting)
        # Start recording — this is fast (just spawns ffmpeg), safe on main thread
        self.on_start_recording(meeting)

    def _on_manual_start(self):
        try:
            from calendar_watcher import Meeting
            manual = Meeting(
                title="Manual Recording",
                start=datetime.now().astimezone(),
                end=datetime.now().astimezone(),
            )
            log.info("UI: manual start clicked")
            self._on_start(manual)
        except Exception as e:
            log.error(f"Failed to start manual recording: {e}")
            import traceback
            traceback.print_exc()

    def _on_stop(self):
        self._signals.set_processing.emit()
        import threading
        threading.Thread(target=self.on_stop_recording, daemon=True).start()

    def _open_folder(self):
        meetings_dir = Path("~/meetings").expanduser()
        meetings_dir.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", str(meetings_dir)])

    def closeEvent(self, event):
        """Hide to tray instead of quitting."""
        self.hide()
        event.ignore()

    # ------------------------------------------------------------------ #
    # Timer                                                                #
    # ------------------------------------------------------------------ #

    def _tick_timer(self):
        self._timer_seconds += 1
        m = self._timer_seconds // 60
        s = self._timer_seconds % 60
        if hasattr(self, "_timer_label"):
            self._timer_label.setText(f"{m:02d}:{s:02d}")

    def _update_status(self, text: str, object_name: str):
        self._status_label.setText(text)
        self._status_label.setObjectName(object_name)
        # Force stylesheet refresh for the new object name
        self._status_label.style().unpolish(self._status_label)
        self._status_label.style().polish(self._status_label)
        # Update tray icon
        if hasattr(self, "_tray") and self._tray:
            color = _STATE_COLORS.get(self._state, "#3A8C5A")
            self._tray.setIcon(_make_leaf_icon(color))


# ---------------------------------------------------------------------------
# Application wrapper — single instance, dock icon, tray
# ---------------------------------------------------------------------------
class AloeScribeApp:
    """
    Wraps AloeScribeWindow in a QApplication with:
    - macOS dock icon (aloe leaf)
    - Menu bar tray icon (QSystemTrayIcon)
    - Single-instance via QLocalServer
    """

    def __init__(self, on_start_recording, on_stop_recording, on_quit):
        self._on_start_recording = on_start_recording
        self._on_stop_recording = on_stop_recording
        self._on_quit = on_quit
        self._app: Optional[QApplication] = None
        self._window: Optional[AloeScribeWindow] = None
        self._tray: Optional[QSystemTrayIcon] = None

    def run(self):
        # Set up as GUI app BEFORE creating QApplication
        _setup_macos_app()

        self._app = QApplication(sys.argv)
        self._app.setApplicationName("Aloe Scribe")
        self._app.setApplicationDisplayName("Aloe Scribe")

        # Dock icon
        # Find icon — works both in dev and inside .app bundle
        if getattr(sys, "frozen", False):
            icon_path = Path(sys.executable).parent.parent / "Resources" / "assets" / "icon.png"
        else:
            icon_path = Path(__file__).parent.parent / "assets" / "icon.png"
        if icon_path.exists():
            self._app.setWindowIcon(QIcon(str(icon_path)))
        else:
            self._app.setWindowIcon(_make_leaf_icon())

        # Don't quit when window is closed (hidden to tray)
        self._app.setQuitOnLastWindowClosed(False)

        # Create window
        self._window = AloeScribeWindow(
            on_start_recording=self._on_start_recording,
            on_stop_recording=self._on_stop_recording,
            on_quit=self._on_quit,
        )

        # System tray
        self._setup_tray()
        self._window._tray = self._tray

        # Register tray for notifications
        import notifications
        notifications.set_tray_icon(self._tray)

        # Re-activate on dock icon click
        self._app.applicationStateChanged.connect(self._on_app_state_changed)

        # Show window and bring to front
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

        sys.exit(self._app.exec())

    def _setup_tray(self):
        self._tray = QSystemTrayIcon(_make_leaf_icon(), self._app)
        self._tray.setToolTip("Aloe Scribe")
        self._tray.activated.connect(self._on_tray_activated)
        self._update_tray_menu()
        self._tray.show()

    def _update_tray_menu(self):
        menu = QMenu()

        # Status header
        status_map = {
            "idle": "Aloe Scribe — Idle",
            "notifying": "Meeting soon",
            "recording": "Recording...",
            "processing": "Transcribing...",
            "done": "Done — transcript saved",
        }
        state = self._window._state if self._window else "idle"
        header = menu.addAction(status_map.get(state, "Aloe Scribe"))
        header.setEnabled(False)

        menu.addSeparator()

        show_action = menu.addAction("Show Window")
        show_action.triggered.connect(self._show_window)

        folder_action = menu.addAction("Open Meetings Folder")
        folder_action.triggered.connect(self._open_folder)

        menu.addSeparator()

        quit_action = menu.addAction("Quit Aloe Scribe")
        quit_action.triggered.connect(self._quit)

        self._tray.setContextMenu(menu)

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._toggle_window()

    def _on_app_state_changed(self, state):
        """Handle dock icon clicks — re-show window when app is activated."""
        from PyQt6.QtCore import Qt as QtCore_Qt
        if state == Qt.ApplicationState.ApplicationActive and self._window:
            self._show_window()

    def _toggle_window(self):
        if self._window.isVisible():
            self._window.hide()
        else:
            self._show_window()

    def _show_window(self):
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    def _open_folder(self):
        meetings_dir = Path("~/meetings").expanduser()
        meetings_dir.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", str(meetings_dir)])

    def _quit(self):
        self._on_quit()
        if self._tray:
            self._tray.hide()
        self._app.quit()

    # ------------------------------------------------------------------ #
    # Proxy state methods (called by main.py)                              #
    # ------------------------------------------------------------------ #

    def notify_upcoming(self, meeting):
        if self._window:
            self._window.notify_upcoming(meeting)
            self._update_tray_menu()
            # Also send a notification
            import notifications
            notifications.send(
                "Aloe Scribe",
                f"Meeting in ~4 min: {meeting.title}",
            )

    def set_recording(self, meeting):
        if self._window:
            self._window.set_recording(meeting)
            self._update_tray_menu()

    def set_processing(self):
        if self._window:
            self._window.set_processing()
            self._update_tray_menu()

    def set_done(self, output_path):
        if self._window:
            self._window.set_done(output_path)
            self._update_tray_menu()
            import notifications
            notifications.send(
                "Aloe Scribe — Done",
                f"Transcript saved: {output_path.name}",
            )

    def set_idle(self):
        if self._window:
            self._window.set_idle()
            self._update_tray_menu()
