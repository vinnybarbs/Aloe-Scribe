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
    QComboBox,
    QProgressBar,
    QFileDialog,
    QTextEdit,
)

from audio_meter import make_avfoundation_meter, make_sck_meter

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
    QLabel#deviceLabel {
        font-size: 10px;
        color: #888888;
        letter-spacing: 1px;
    }
    QComboBox {
        font-size: 11px;
        padding: 4px 8px;
        border: 1px solid #cccccc;
        border-radius: 4px;
    }
"""


# ---------------------------------------------------------------------------
# Signals bridge — thread-safe state updates from background threads
# ---------------------------------------------------------------------------
class _Signals(QObject):
    set_recording = pyqtSignal(object)
    set_processing = pyqtSignal()
    set_done = pyqtSignal(object)
    set_idle = pyqtSignal()
    mic_level = pyqtSignal(float)
    sys_level = pyqtSignal(float)
    live_preview_append = pyqtSignal(str)
    live_preview_clear = pyqtSignal()
    live_preview_status = pyqtSignal(str)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class AloeScribeWindow(QMainWindow):
    def __init__(
        self,
        on_start_recording: Callable,
        on_stop_recording: Callable,
        on_quit: Callable,
        list_sources: Callable = None,
        on_device_change: Callable = None,
        on_output_dir_change: Callable = None,
        on_transcribe_file: Callable = None,
        live_preview: bool = False,
        current_mic: str = "",
        current_system: str = "",
        current_output_dir: str = "",
    ):
        super().__init__()
        self.on_start_recording = on_start_recording
        self.on_stop_recording = on_stop_recording
        self.on_quit = on_quit
        self._list_sources = list_sources
        self._on_device_change = on_device_change
        self._on_output_dir_change = on_output_dir_change
        self._on_transcribe_file = on_transcribe_file
        self._live_preview_enabled = live_preview
        self._selected_mic = current_mic
        self._selected_system = current_system
        self._output_dir = current_output_dir

        self._current_meeting = None
        self._state = "idle"
        self._timer_seconds = 0
        self._processing_seconds = 0
        self._processing_timer = None
        self._quit_after_transcribe = False
        self._mic_meter = None
        self._sys_meter = None
        self._mic_level_bar: Optional[QProgressBar] = None
        self._sys_level_bar: Optional[QProgressBar] = None
        self._sys_warn_label: Optional[QLabel] = None
        self._live_preview_box: Optional[QTextEdit] = None

        # Signals for thread-safe updates
        self._signals = _Signals()
        self._signals.set_recording.connect(self._render_recording)
        self._signals.set_processing.connect(self._render_processing)
        self._signals.set_done.connect(self._render_done)
        self._signals.set_idle.connect(self._render_idle)
        self._signals.mic_level.connect(self._update_mic_level)
        self._signals.sys_level.connect(self._update_sys_level)
        self._signals.live_preview_append.connect(self._on_live_preview_append)
        self._signals.live_preview_clear.connect(self._on_live_preview_clear)
        self._signals.live_preview_status.connect(self._on_live_preview_status)

        # Timer
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick_timer)

        # Window setup
        self.setWindowTitle("Aloe Scribe")
        self.setFixedSize(300, 460)
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
        # Stop meters before tearing down their bar widgets
        self._stop_meters()
        self._mic_level_bar = None
        self._sys_level_bar = None
        self._sys_warn_label = None
        self._live_preview_box = None
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
    # Audio meters                                                         #
    # ------------------------------------------------------------------ #

    def _system_on(self) -> bool:
        """True when system-audio capture is enabled (not explicitly off)."""
        v = (self._selected_system or "").strip().lower()
        return v not in {"off", "none", "false", "0", "no", "disabled"}

    def _build_level_bars(self):
        """Add the MIC LEVEL bar, and — when system capture is on — a SYSTEM
        AUDIO LEVEL bar plus a warning. The system bar is driven by an SCK
        meter while idle so the user can SEE background media (e.g. a YouTube
        tab) being captured BEFORE recording: ScreenCaptureKit grabs the whole
        system mix, so anything playing lands in the recording."""
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        self._content_layout.addWidget(sep)

        mic_label = QLabel("MIC LEVEL")
        mic_label.setObjectName("deviceLabel")
        self._content_layout.addWidget(mic_label)

        self._mic_level_bar = QProgressBar()
        self._mic_level_bar.setRange(0, 1000)
        self._mic_level_bar.setValue(0)
        self._mic_level_bar.setTextVisible(False)
        self._mic_level_bar.setFixedHeight(8)
        self._content_layout.addWidget(self._mic_level_bar)

        if self._system_on():
            sys_label = QLabel("SYSTEM AUDIO LEVEL")
            sys_label.setObjectName("deviceLabel")
            self._content_layout.addWidget(sys_label)

            self._sys_level_bar = QProgressBar()
            self._sys_level_bar.setRange(0, 1000)
            self._sys_level_bar.setValue(0)
            self._sys_level_bar.setTextVisible(False)
            self._sys_level_bar.setFixedHeight(8)
            self._content_layout.addWidget(self._sys_level_bar)

            self._sys_warn_label = QLabel("")
            self._sys_warn_label.setWordWrap(True)
            self._sys_warn_label.setStyleSheet("color: #C94040; font-size: 10px;")
            self._sys_warn_label.setVisible(False)
            self._content_layout.addWidget(self._sys_warn_label)
        else:
            self._sys_level_bar = None
            self._sys_warn_label = None

    def _start_meters(self):
        """Spawn meter readers for mic (avfoundation/ffmpeg) and system
        (ScreenCaptureKit via the Swift helper). The system meter no longer
        relies on BlackHole — it taps directly into the same path the recorder
        uses, so the bar moves whenever audio is actually being captured."""
        if not self.isVisible():
            return
        self._stop_meters()

        try:
            from recorder_mac import _resolve_device, _find_default_mic, _helper_path
        except Exception as e:
            log.warning(f"Cannot import recorder_mac helpers: {e}")
            return

        # Mic side: still avfoundation/ffmpeg.
        mic_idx = _resolve_device(self._selected_mic) or _find_default_mic()
        if mic_idx:
            self._mic_meter = make_avfoundation_meter(
                mic_idx,
                lambda lvl: self._signals.mic_level.emit(lvl),
            )
            if self._mic_meter and not self._mic_meter.start():
                self._mic_meter = None

        # System-audio meter via the SCK helper in --meter mode. Run it ONLY
        # while idle: during recording the recorder owns the SCStream and a
        # second one fights it. Idle is exactly when the user needs it — to spot
        # background media (YouTube, etc.) before hitting record.
        self._sys_meter = None
        if (self._state != "recording" and self._system_on()
                and self._sys_level_bar is not None):
            try:
                self._sys_meter = make_sck_meter(
                    str(_helper_path()),
                    lambda lvl: self._signals.sys_level.emit(lvl),
                )
                if self._sys_meter and not self._sys_meter.start():
                    self._sys_meter = None
            except Exception as e:
                log.warning(f"Could not start system meter: {e}")
                self._sys_meter = None

    def _stop_meters(self):
        for attr in ("_mic_meter", "_sys_meter"):
            meter = getattr(self, attr, None)
            if meter is not None:
                try:
                    meter.stop()
                except Exception:
                    pass
                setattr(self, attr, None)

    def _restart_meters_if_active(self):
        if self._mic_meter is not None or self._sys_meter is not None:
            self._start_meters()

    def _update_mic_level(self, level: float):
        bar = self._mic_level_bar
        if bar is not None:
            try:
                bar.setValue(int(min(1.0, max(0.0, level)) * 1000))
            except Exception:
                pass

    def _update_sys_level(self, level: float):
        bar = self._sys_level_bar
        if bar is not None:
            try:
                bar.setValue(int(min(1.0, max(0.0, level)) * 1000))
            except Exception:
                pass
        # Warn when something loud is playing into the system mix at idle, so the
        # user closes it before it gets recorded. Threshold ~8% peak.
        warn = self._sys_warn_label
        if warn is not None:
            try:
                if self._state != "recording" and level > 0.08:
                    warn.setText("⚠️ Audio is playing on your Mac — it will be "
                                 "recorded. Close it (YouTube, music, video) first.")
                    warn.setVisible(True)
                else:
                    warn.setVisible(False)
            except Exception:
                pass

    def hideEvent(self, event):
        # Save CPU when the window is hidden to the menu bar
        self._stop_meters()
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if self._state in ("idle", "recording") and self._mic_level_bar is not None:
            self._start_meters()

    # ------------------------------------------------------------------ #
    # State renderers                                                       #
    # ------------------------------------------------------------------ #

    def _render_idle(self):
        # If the user requested quit during a recording, bail out once transcription completes.
        if self._quit_after_transcribe:
            app_ref = getattr(self, "_app_ref", None)
            if app_ref is not None:
                app_ref._final_quit()
                return
        self._state = "idle"
        self._timer.stop()
        self._stop_processing_timer()
        self._clear_content()

        label = QLabel("Ready when you are.")
        label.setObjectName("meetingTime")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(label)

        sub = QLabel("Click below to start capturing audio.")
        sub.setObjectName("stateLabel")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(sub)

        # Audio device dropdowns
        if self._list_sources:
            self._build_device_dropdowns()

        # Live level meters so the user can verify audio is flowing
        self._build_level_bars()

        # Output folder picker
        self._build_output_folder_row()

        # Recovery dropdown: pick an un-transcribed recording and run it.
        # Only appears when such WAVs exist; grow the window to fit it.
        has_recordings = self._build_transcribe_file_row()
        base = 510 if self._system_on() else 460  # +50 for the system-audio meter
        self.setFixedSize(300, base + (80 if has_recordings else 0))

        btn = QPushButton("Start Recording Now")
        btn.setObjectName("btnStart")
        btn.clicked.connect(self._on_manual_start)
        # Brief debounce: disable the Start button for 600 ms so a residual
        # click from the previous screen (Done → idle) doesn't immediately
        # fire a new recording at the same on-screen coordinate.
        btn.setEnabled(False)
        QTimer.singleShot(600, lambda b=btn: b.setEnabled(True))
        self._content_layout.addWidget(btn)

        self._update_status("● IDLE", "statusIdle")
        self._start_meters()

    def _build_device_dropdowns(self):
        """Add mic and speaker/system audio dropdowns to the idle view."""
        try:
            mics, systems = self._list_sources()
        except Exception as e:
            log.warning(f"Could not list audio devices: {e}")
            return

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        self._content_layout.addWidget(sep)

        # Mic dropdown
        mic_label = QLabel("MICROPHONE")
        mic_label.setObjectName("deviceLabel")
        self._content_layout.addWidget(mic_label)

        mic_combo = QComboBox()
        mic_combo.addItem("Auto-detect", "")
        active_mic_idx = 0
        for i, (dev_id, display) in enumerate(mics):
            mic_combo.addItem(display, dev_id)
            if dev_id == self._selected_mic:
                active_mic_idx = i + 1
        mic_combo.setCurrentIndex(active_mic_idx)
        mic_combo.currentIndexChanged.connect(
            lambda idx, c=mic_combo: self._on_mic_changed(c)
        )
        self._content_layout.addWidget(mic_combo)

        # System audio dropdown — explicit on/off rather than the legacy
        # "Auto-detect / BlackHole" device picker. SCK always captures the
        # full desktop mix when on; off is for in-person meetings where the
        # mic already picks up everyone in the room.
        sys_label = QLabel("SYSTEM AUDIO")
        sys_label.setObjectName("deviceLabel")
        self._content_layout.addWidget(sys_label)

        sys_combo = QComboBox()
        sys_options = [
            ("system", "On — Capture system audio"),
            ("off", "Off — Mic only (in-person)"),
        ]
        # Default to "On" unless config explicitly says off.
        current = (self._selected_system or "").strip().lower()
        if current in {"off", "none", "false", "0", "no", "disabled"}:
            active_sys_idx = 1
        else:
            active_sys_idx = 0
        for dev_id, display in sys_options:
            sys_combo.addItem(display, dev_id)
        sys_combo.setCurrentIndex(active_sys_idx)
        sys_combo.currentIndexChanged.connect(
            lambda idx, c=sys_combo: self._on_sys_changed(c)
        )
        self._content_layout.addWidget(sys_combo)

    def _on_mic_changed(self, combo):
        self._selected_mic = combo.currentData() or ""
        if self._on_device_change:
            self._on_device_change(self._selected_mic, self._selected_system)
        self._restart_meters_if_active()

    def _on_sys_changed(self, combo):
        self._selected_system = combo.currentData() or ""
        if self._on_device_change:
            self._on_device_change(self._selected_mic, self._selected_system)
        self._restart_meters_if_active()

    # ------------------------------------------------------------------ #
    # Output folder picker                                                #
    # ------------------------------------------------------------------ #

    def _build_output_folder_row(self):
        """Show the current transcript destination and a Choose… button.

        We display only the leaf folder name (not the full OneDrive path),
        keep it on a single elided line, and put the full path on hover.
        That keeps the row tidy regardless of how deep the user's
        destination lives."""
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        self._content_layout.addWidget(sep)

        label = QLabel("SAVE TRANSCRIPTS TO")
        label.setObjectName("deviceLabel")
        self._content_layout.addWidget(label)

        row = QHBoxLayout()
        full_path = self._output_dir or "~/meetings"
        short, tooltip = self._pretty_output_path(full_path)
        self._output_dir_label = QLabel(short)
        self._output_dir_label.setObjectName("meetingTime")
        self._output_dir_label.setWordWrap(False)
        self._output_dir_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._output_dir_label.setToolTip(tooltip)
        # Elide on the right if even the short form runs out of room.
        self._output_dir_label.setSizePolicy(
            self._output_dir_label.sizePolicy().horizontalPolicy(),
            self._output_dir_label.sizePolicy().verticalPolicy(),
        )
        row.addWidget(self._output_dir_label, 1)

        choose = QPushButton("Choose…")
        choose.setObjectName("btnSkip")
        # No max width — let Qt size it to the text + button padding so the
        # ellipsis character isn't clipped to "Choose..".
        choose.setMinimumWidth(96)
        choose.clicked.connect(self._choose_output_folder)
        row.addWidget(choose, 0)

        self._content_layout.addLayout(row)

    @staticmethod
    def _pretty_output_path(full: str) -> tuple[str, str]:
        """Turn '/Users/me/Library/CloudStorage/OneDrive-X/.../Foo' into
        a short display string + full-path tooltip."""
        path = Path(full).expanduser()
        # Tooltip: shrink $HOME to ~ for readability.
        home = str(Path.home())
        full_str = str(path)
        tooltip = "~" + full_str[len(home):] if full_str.startswith(home) else full_str
        # Short label: parent · leaf if the parent looks meaningful, else
        # just the leaf, else ~ if it IS the home dir.
        if path == Path.home():
            return "~", tooltip
        leaf = path.name or full_str
        parent = path.parent.name
        # Surface "OneDrive-Foo" as the parent context when it's nested
        # under CloudStorage. Otherwise show just the leaf — it's enough.
        if "CloudStorage" in full_str and parent and parent != "CloudStorage":
            # e.g. ".../OneDrive-Trace3/My Documents/AloeScribe Transcriptions"
            # → "OneDrive · AloeScribe Transcriptions"
            cloud_segment = next(
                (p for p in path.parts if p.startswith("OneDrive")
                 or p.startswith("GoogleDrive") or p.startswith("Dropbox")),
                None,
            )
            if cloud_segment:
                provider = cloud_segment.split("-", 1)[0]
                return f"{provider} · {leaf}", tooltip
        return leaf, tooltip

    def _choose_output_folder(self):
        start = Path(self._output_dir or "~/meetings").expanduser()
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Choose where Aloe Scribe saves transcripts",
            str(start),
        )
        if not chosen:
            return
        self._output_dir = chosen
        # Re-pretty-print so the row stays compact and the tooltip shows
        # the full path.
        if getattr(self, "_output_dir_label", None) is not None:
            short, tooltip = self._pretty_output_path(chosen)
            self._output_dir_label.setText(short)
            self._output_dir_label.setToolTip(tooltip)
        if self._on_output_dir_change:
            self._on_output_dir_change(chosen)
        # Folder changed — the set of recoverable recordings likely changed too.
        # Re-render the idle screen so the transcribe dropdown reflects it.
        self._render_idle()

    def _build_transcribe_file_row(self) -> bool:
        """Show a dropdown of un-transcribed recordings + a Transcribe button.

        Lists only .wav files in the output folder that don't yet have a .md
        sibling — i.e. recordings whose transcription failed or was
        interrupted. Returns True if the row was added (recordings exist)."""
        if not self._on_transcribe_file:
            return False
        out_dir = (
            Path(self._output_dir).expanduser()
            if self._output_dir
            else Path("~/meetings").expanduser()
        )
        try:
            wavs = sorted(
                w for w in out_dir.glob("*.wav") if not w.with_suffix(".md").exists()
            )
        except Exception:
            wavs = []
        if not wavs:
            self._wav_combo = None
            return False

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        self._content_layout.addWidget(sep)

        label = QLabel("TRANSCRIBE A RECORDING")
        label.setObjectName("deviceLabel")
        self._content_layout.addWidget(label)

        row = QHBoxLayout()
        self._wav_combo = QComboBox()
        for w in wavs:
            self._wav_combo.addItem(w.name, str(w))
        row.addWidget(self._wav_combo, 1)

        go = QPushButton("Transcribe")
        go.setObjectName("btnSkip")
        go.setMinimumWidth(96)
        go.clicked.connect(self._on_transcribe_clicked)
        row.addWidget(go, 0)

        self._content_layout.addLayout(row)
        return True

    def _on_transcribe_clicked(self):
        combo = getattr(self, "_wav_combo", None)
        if combo is None:
            return
        wav = combo.currentData()
        if not wav or not self._on_transcribe_file:
            return
        log.info(f"UI: transcribe existing recording: {wav}")
        # Show the processing screen, then run transcription off the UI thread.
        self._signals.set_processing.emit()
        import threading

        threading.Thread(
            target=lambda: self._on_transcribe_file(wav), daemon=True
        ).start()

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

        # Live level meters so the user can verify audio is flowing
        self._build_level_bars()

        # Live transcription preview (first ~2 min) — proof the capture →
        # transcribe pipeline is actually working, not just that audio flows.
        if self._live_preview_enabled:
            prev_label = QLabel("LIVE TRANSCRIPT · first 2 min")
            prev_label.setObjectName("deviceLabel")
            self._content_layout.addWidget(prev_label)

            self._live_preview_box = QTextEdit()
            self._live_preview_box.setReadOnly(True)
            self._live_preview_box.setFixedHeight(70)
            self._live_preview_box.setPlaceholderText(
                "A transcript sample will appear here at ~20s — confirms it's working."
            )
            self._live_preview_box.setStyleSheet(
                "font-size: 10px; color: #333; background: #f5f7f5;"
                " border: 1px solid #d8e0d8; border-radius: 4px;"
            )
            self._content_layout.addWidget(self._live_preview_box)

        stop = QPushButton("Stop & Transcribe")
        stop.setObjectName("btnStop")
        stop.clicked.connect(self._on_stop)
        self._content_layout.addWidget(stop)

        self.setFixedSize(300, 560 if self._live_preview_enabled else 460)
        self._update_status("● RECORDING", "statusRecord")
        self._timer_seconds = 0
        self._timer.start()
        self._start_meters()

    def _on_live_preview_append(self, text: str):
        box = self._live_preview_box
        if box is None or not text:
            return
        try:
            box.append(text)
            sb = box.verticalScrollBar()
            sb.setValue(sb.maximum())
        except Exception:
            pass

    def _on_live_preview_clear(self):
        box = self._live_preview_box
        if box is not None:
            try:
                box.clear()
            except Exception:
                pass

    def _on_live_preview_status(self, msg: str):
        # Shown via the placeholder, so it's visible only until real transcript
        # text arrives (then the box has content and the placeholder hides).
        box = self._live_preview_box
        if box is not None:
            try:
                box.setPlaceholderText(msg)
            except Exception:
                pass

    def _render_processing(self):
        self._state = "processing"
        self._timer.stop()
        self._stop_processing_timer()
        self._clear_content()

        stopped = QLabel("RECORDING STOPPED")
        stopped.setObjectName("stateLabel")
        stopped.setStyleSheet("color: #3A8C5A;")
        stopped.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(stopped)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        self._content_layout.addWidget(sep)

        label = QLabel("Transcribing audio...")
        label.setObjectName("meetingTime")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(label)

        hint = QLabel("Long recordings may take several minutes.")
        hint.setObjectName("stateLabel")
        # Smaller font + tighter letter-spacing so the line isn't clipped at the
        # window edges; word-wrap as a safety net on narrow widths.
        hint.setStyleSheet("font-size: 9px; letter-spacing: 1px;")
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(hint)

        self._processing_timer_label = QLabel("00:00")
        self._processing_timer_label.setObjectName("timer")
        self._processing_timer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(self._processing_timer_label)

        self._update_status("● PROCESSING", "statusProcess")

        # Start a processing elapsed timer so the user can see it's alive
        self._processing_seconds = 0
        self._processing_timer = QTimer(self)
        self._processing_timer.setInterval(1000)
        self._processing_timer.timeout.connect(self._tick_processing_timer)
        self._processing_timer.start()

    def _render_done(self, output_path):
        self._state = "done"
        self._stop_processing_timer()
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

        # If we were waiting to quit after transcription, do that now.
        # Otherwise wait for the user to explicitly click Done — the previous
        # 10s auto-flip back to idle was firing a fresh Start button right
        # where the user was still clicking, causing accidental new
        # recordings.
        if self._quit_after_transcribe:
            QTimer.singleShot(3000, self._signals.set_idle.emit)

    # ------------------------------------------------------------------ #
    # Public state setters (thread-safe via signals)                       #
    # ------------------------------------------------------------------ #

    def set_recording(self, meeting):
        self._signals.set_recording.emit(meeting)

    def set_processing(self):
        self._signals.set_processing.emit()

    def set_done(self, output_path):
        self._signals.set_done.emit(output_path)

    def set_idle(self):
        self._signals.set_idle.emit()

    def live_preview_append(self, text):
        self._signals.live_preview_append.emit(text)

    def live_preview_clear(self):
        self._signals.live_preview_clear.emit()

    def live_preview_status(self, msg):
        self._signals.live_preview_status.emit(msg)

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
            from meeting import Meeting
            manual = Meeting(title="Manual Recording")
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
        # Open the user's configured transcript destination, falling back to
        # ~/meetings if the path is empty or invalid.
        path = Path(self._output_dir).expanduser() if self._output_dir else Path("~/meetings").expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning(f"Could not create {path}: {e}")
            path = Path("~/meetings").expanduser()
            path.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", str(path)])

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

    def _tick_processing_timer(self):
        self._processing_seconds += 1
        m = self._processing_seconds // 60
        s = self._processing_seconds % 60
        if hasattr(self, "_processing_timer_label"):
            self._processing_timer_label.setText(f"{m:02d}:{s:02d}")

    def _stop_processing_timer(self):
        if self._processing_timer:
            self._processing_timer.stop()
            self._processing_timer = None

    def _update_status(self, text: str, object_name: str):
        self._status_label.setText(text)
        self._status_label.setObjectName(object_name)
        # Force stylesheet refresh for the new object name
        self._status_label.style().unpolish(self._status_label)
        self._status_label.style().polish(self._status_label)
        # Refresh the tray menu so the header text ("Recording…", "Transcribing…",
        # …) reflects the new state. We intentionally do NOT swap the tray
        # icon per-state: state-colored procedural QPixmaps don't render as
        # macOS menu-bar status items, so we keep the static PNG and surface
        # state via the menu header instead.
        if hasattr(self, "_app_ref") and self._app_ref:
            self._app_ref._update_tray_menu()


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

    def __init__(self, on_start_recording, on_stop_recording, on_quit,
                 list_sources=None, on_device_change=None,
                 on_output_dir_change=None, on_transcribe_file=None,
                 live_preview=False,
                 current_mic="", current_system="", current_output_dir=""):
        self._on_start_recording = on_start_recording
        self._on_stop_recording = on_stop_recording
        self._on_quit = on_quit
        self._list_sources = list_sources
        self._on_device_change = on_device_change
        self._on_output_dir_change = on_output_dir_change
        self._on_transcribe_file = on_transcribe_file
        self._live_preview = live_preview
        self._current_mic = current_mic
        self._current_system = current_system
        self._current_output_dir = current_output_dir
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
            list_sources=self._list_sources,
            on_device_change=self._on_device_change,
            on_output_dir_change=self._on_output_dir_change,
            on_transcribe_file=self._on_transcribe_file,
            live_preview=self._live_preview,
            current_mic=self._current_mic,
            current_system=self._current_system,
            current_output_dir=self._current_output_dir,
        )

        # System tray
        self._setup_tray()
        self._window._tray = self._tray
        self._window._app_ref = self

        # NativeTray doesn't expose a showMessage hook — notifications.py
        # will fall through to osascript's `display notification` path,
        # which is what we want on macOS anyway.
        import notifications
        notifications.set_tray_icon(None)

        # Re-activate on dock icon click
        self._app.applicationStateChanged.connect(self._on_app_state_changed)

        # Show window and bring to front
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

        sys.exit(self._app.exec())

    def _setup_tray(self):
        # We bypass Qt's QSystemTrayIcon — it silently fails to draw on
        # macOS Tahoe even when isVisible() reports True — and go straight
        # to NSStatusItem via PyObjC. See src/native_tray.py.
        if getattr(sys, "frozen", False):
            icon_path = Path(sys.executable).parent.parent / "Resources" / "assets" / "icon.png"
        else:
            icon_path = Path(__file__).parent.parent / "assets" / "icon.png"
        try:
            from native_tray import NativeTray
        except Exception as e:
            log.warning(f"Could not import NativeTray: {e}")
            self._tray = None
            return
        self._tray = NativeTray(
            icon_path=icon_path,
            on_show=self._show_window,
            on_open_folder=self._open_folder,
            on_quit=self._quit,
            on_toggle=self._toggle_window,
        )

    def _update_tray_menu(self):
        # The NativeTray rebuilds its own NSMenu whenever the status text
        # changes; we just feed it the current state string.
        if self._tray is None:
            return
        status_map = {
            "idle": "Aloe Scribe — Idle",
            "recording": "Recording…",
            "processing": "Transcribing…",
            "done": "Done — transcript saved",
        }
        state = self._window._state if self._window else "idle"
        try:
            self._tray.set_status_text(status_map.get(state, "Aloe Scribe"))
        except Exception as e:
            log.warning(f"tray update failed: {e}")

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
        # Defer to the window's open-folder so we always honor the user's
        # current transcript destination (which the folder picker mutates
        # live on _output_dir).
        if self._window is not None:
            self._window._open_folder()
            return
        # Fallback if the window isn't up yet.
        path = Path(self._current_output_dir).expanduser() if self._current_output_dir else Path("~/meetings").expanduser()
        path.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", str(path)])

    def _quit(self):
        # If recording is in progress, stop + transcribe first, then quit.
        if self._window and self._window._state == "recording":
            log.info("Quit requested during recording — stopping + transcribing first")
            self._window._quit_after_transcribe = True
            self._window._on_stop()
            return
        self._on_quit()
        # NSStatusItem cleans itself up when the process exits; no explicit
        # hide() is needed (and the legacy QSystemTrayIcon .hide() call would
        # AttributeError against the new NativeTray).
        self._app.quit()

    def _final_quit(self):
        self._on_quit()
        if self._app:
            self._app.quit()

    # ------------------------------------------------------------------ #
    # Proxy state methods (called by main.py)                              #
    # ------------------------------------------------------------------ #

    def set_recording(self, meeting):
        if self._window:
            self._window.set_recording(meeting)

    def set_processing(self):
        if self._window:
            self._window.set_processing()

    def set_done(self, output_path):
        if self._window:
            self._window.set_done(output_path)
            import notifications
            notifications.send(
                "Aloe Scribe — Done",
                f"Transcript saved: {output_path.name}",
            )

    def set_idle(self):
        if self._window:
            self._window.set_idle()

    def live_preview_append(self, text):
        if self._window:
            self._window.live_preview_append(text)

    def live_preview_clear(self):
        if self._window:
            self._window.live_preview_clear()

    def live_preview_status(self, msg):
        if self._window:
            self._window.live_preview_status(msg)
