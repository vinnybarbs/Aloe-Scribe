"""
ui_mac.py — Aloe Scribe macOS UI using PyQt6.

Provides a floating window, dock icon, and system tray icon (menu bar).
Single-instance: re-launching activates the existing window.
"""

import logging
import subprocess
import sys
import threading
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
    QMessageBox,
    QTextEdit,
    QDialog,
    QDialogButtonBox,
    QLineEdit,
    QScrollArea,
    QSplitter,
    QPlainTextEdit,
    QInputDialog,
    QCompleter,
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
    /* Aloe palette: green #2F8F5B · ink #1E2A23 · muted #8B948C
       hairline #E2E8E3 · input surface #F5F8F5 */
    QMainWindow, QWidget#central { background-color: #FFFFFF; }

    QLabel#appTitle { font-size: 16px; font-weight: 600; color: #1E2A23; }
    QLabel#appSub   { font-size: 16px; font-weight: 400; color: #2F8F5B; }

    QLabel#stateLabel   { font-size: 12px; color: #9AA39C; }
    QLabel#meetingTitle { font-size: 15px; font-weight: 600; color: #1E2A23; }
    QLabel#meetingTime  { font-size: 12px; color: #9AA39C; }
    QLabel#timer {
        font-size: 34px; font-weight: 600; color: #1E2A23;
        font-family: "SF Mono", "Menlo", "Courier New", monospace;
    }
    QLabel#statusIdle    { color: #8B948C; font-size: 12px; }
    QLabel#statusRecord  { color: #C2412F; font-size: 12px; }
    QLabel#statusProcess { color: #3878C8; font-size: 12px; }
    QLabel#statusDone    { color: #2F8F5B; font-size: 12px; }
    QLabel#deviceLabel   { font-size: 12px; color: #8B948C; }
    QLabel#finePrint     { font-size: 10px; color: #A6AEA8; }

    QPushButton#btnStart {
        background-color: #2F8F5B; color: #FFFFFF; border: none;
        border-radius: 12px; font-weight: 600; font-size: 14px; padding: 13px 16px;
    }
    QPushButton#btnStart:hover    { background-color: #276F48; }
    QPushButton#btnStart:disabled { background-color: #BFD6C7; }

    QPushButton#btnStop {
        background-color: #C2412F; color: #FFFFFF; border: none;
        border-radius: 12px; font-weight: 600; font-size: 14px; padding: 13px 16px;
    }
    QPushButton#btnStop:hover { background-color: #A23526; }

    QPushButton#btnSkip {
        background-color: transparent; color: #2F8F5B;
        border: 1px solid #CFE0D4; border-radius: 8px; font-size: 12px; padding: 6px 14px;
    }
    QPushButton#btnSkip:hover { background-color: #F0F6F1; }

    QFrame#separator { background-color: #EEF2EE; max-height: 1px; border: none; }

    QComboBox {
        font-size: 13px; color: #1E2A23;
        background-color: #F5F8F5;
        border: 1px solid #E2E8E3; border-radius: 10px;
        padding: 9px 12px;
    }
    QComboBox:hover { border: 1px solid #CFE0D4; }
    QComboBox:focus { border: 1px solid #2F8F5B; }
    QComboBox::drop-down { border: none; width: 26px; }
    QComboBox::down-arrow {
        image: none;
        border-left: 4px solid transparent; border-right: 4px solid transparent;
        border-top: 5px solid #A6AEA8; margin-right: 12px; width: 0; height: 0;
    }
    QComboBox QAbstractItemView {
        background-color: #FFFFFF; color: #1E2A23;
        border: 1px solid #E2E8E3; border-radius: 8px; padding: 4px; outline: none;
        selection-background-color: #EAF3EE; selection-color: #1E2A23;
    }

    QProgressBar {
        background-color: #EEF2EE; border: none; border-radius: 3px; max-height: 6px;
    }
    QProgressBar::chunk { background-color: #5FB587; border-radius: 3px; }

    QTextEdit {
        background-color: #F5F8F5; color: #333333;
        border: 1px solid #E2E8E3; border-radius: 10px; font-size: 11px; padding: 6px;
    }

    QLineEdit {
        font-size: 13px; color: #1E2A23; background-color: #F5F8F5;
        border: 1px solid #E2E8E3; border-radius: 10px; padding: 9px 12px;
    }
    QLineEdit:focus { border: 1px solid #2F8F5B; }

    /* Notes window — notepad-first: the user's words are the product (ink,
       roomy type on white); the machine's words are the quiet grey strip. */
    QWidget#notesRoot { background-color: #FFFFFF; }
    QPlainTextEdit#notesPad {
        background-color: #FFFFFF; color: #1E2A23;
        border: none; font-size: 14px; line-height: 1.5; padding: 8px 4px;
    }
    QPlainTextEdit#transcriptStrip {
        background-color: #F5F8F5; color: #8B948C;
        border: 1px solid #E2E8E3; border-radius: 10px;
        font-size: 11px; padding: 8px;
    }
    QSplitter::handle { background-color: #EEF2EE; height: 3px; }
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
    live_preview_set = pyqtSignal(str)
    live_preview_clear = pyqtSignal()
    live_preview_status = pyqtSignal(str)
    show_error = pyqtSignal(str)
    prompt_speaker_names = pyqtSignal(object)  # (quotes, apply_callback)
    processing_status = pyqtSignal(str)
    processing_draft = pyqtSignal(str)
    notes_final = pyqtSignal(object)  # (path, transcript_text)


# ---------------------------------------------------------------------------
# Meeting notes window — live notepad + speaker tags + transcript pane
# ---------------------------------------------------------------------------
class NotesWindow(QWidget):
    """Side panel for the meeting itself.

    Top pane: the transcript. During recording it mirrors the live stream
    (read-only, and it only autoscrolls while you're already at the bottom —
    scroll up to re-read and it stays put). Once the final transcript is
    ready it becomes an editable view of the saved file, with one chip
    button per speaker for click-to-rename.

    Bottom pane: a note entry line plus the running notes log, and a
    speaker-tag row. Tagging a name while that person is talking is the
    naming mechanism: the pipeline assigns each diarized cluster the name
    whose tags land inside its speech. Tags beat post-call guessing because
    you could hear the speaker when you tagged them.

    State flows out through two callbacks: on_meta_changed(tags, notes_text)
    fires (debounced) on every change so the app can crash-persist it, and
    on_save(path, text) writes the edited final transcript.
    """

    def __init__(self, on_meta_changed=None, on_save=None):
        super().__init__()
        self._on_meta_changed = on_meta_changed
        self._on_save = on_save
        self._meeting_start: Optional[datetime] = None
        self._tags: list = []          # [(elapsed_seconds, name), ...]
        self._final_path: Optional[Path] = None
        self._known_names: list = []

        self.setWindowTitle("Aloe Scribe — Meeting Notes")
        self.setObjectName("notesRoot")
        self.setStyleSheet(_STYLESHEET)
        self.resize(520, 640)

        layout = QVBoxLayout(self)
        split = QSplitter(Qt.Orientation.Vertical)
        layout.addWidget(split)

        # --- top: transcript ---
        top = QWidget()
        top_l = QVBoxLayout(top)
        self._transcript_caption = QLabel("Live transcript")
        self._transcript_caption.setObjectName("deviceLabel")
        top_l.addWidget(self._transcript_caption)
        self._chips_row = QHBoxLayout()
        top_l.addLayout(self._chips_row)
        self._transcript = QPlainTextEdit()
        self._transcript.setObjectName("transcriptStrip")
        self._transcript.setReadOnly(True)
        self._transcript.setPlaceholderText(
            "The transcript streams here while recording."
        )
        top_l.addWidget(self._transcript)
        save_row = QHBoxLayout()
        self._save_btn = QPushButton("Save transcript edits")
        self._save_btn.setObjectName("btnStart")
        self._save_btn.setVisible(False)
        self._save_btn.clicked.connect(self._save_final)
        save_row.addStretch(1)
        save_row.addWidget(self._save_btn)
        top_l.addLayout(save_row)
        split.addWidget(top)

        # --- bottom: tags + notes ---
        bottom = QWidget()
        bot_l = QVBoxLayout(bottom)

        # Attendees first (a calm-moment task at meeting start), assignment
        # second (a one-click task while someone is talking). Not everyone on
        # the roster will speak — the roster itself still lands in the
        # transcript header.
        att_caption = QLabel("Attendees — add everyone on the call")
        att_caption.setObjectName("deviceLabel")
        bot_l.addWidget(att_caption)
        self._attendee_edit = QLineEdit()
        self._attendee_edit.setPlaceholderText("Name (Enter adds)")
        self._attendee_edit.returnPressed.connect(self._add_attendee)
        # Autocomplete from every attendee ever entered — recurring names
        # come back with two keystrokes, no extra button.
        try:
            import json

            history = [
                n
                for n in json.loads(self._LAST_ATTENDEES.read_text())
                if isinstance(n, str) and n.strip()
            ]
        except Exception:
            history = []
        completer = QCompleter(sorted(set(history)))
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._attendee_edit.setCompleter(completer)
        bot_l.addWidget(self._attendee_edit)
        self._tag_chips = QHBoxLayout()
        bot_l.addLayout(self._tag_chips)

        speak_row = QHBoxLayout()
        speak_caption = QLabel("Who is speaking now:")
        speak_caption.setObjectName("deviceLabel")
        speak_row.addWidget(speak_caption, 0)
        self._speaker_combo = QComboBox()
        self._speaker_combo.setEditable(True)
        self._speaker_combo.lineEdit().setPlaceholderText("Pick an attendee")
        speak_row.addWidget(self._speaker_combo, 1)
        assign_btn = QPushButton("Assign")
        assign_btn.setObjectName("btnSkip")
        assign_btn.clicked.connect(self._assign_current)
        speak_row.addWidget(assign_btn, 0)
        bot_l.addLayout(speak_row)

        self._tag_status = QLabel("")
        self._tag_status.setObjectName("stateLabel")
        bot_l.addWidget(self._tag_status)

        notes_caption = QLabel("Notes — saved at the end of the transcript")
        notes_caption.setObjectName("deviceLabel")
        bot_l.addWidget(notes_caption)
        self._notes_log = QPlainTextEdit()
        self._notes_log.setObjectName("notesPad")
        self._notes_log.setPlaceholderText(
            "Your notes. Rough is fine — a few bullets carry the meeting.\n"
            "Everything here lands in the transcript's Notes section."
        )
        self._notes_log.textChanged.connect(self._schedule_meta_push)
        bot_l.addWidget(self._notes_log)
        split.addWidget(bottom)
        # Notes are the working surface; the transcript is a glanceable
        # strip (drag the splitter for more).
        split.setSizes([170, 470])

        # Debounce for crash-persistence pushes.
        self._meta_timer = QTimer(self)
        self._meta_timer.setSingleShot(True)
        self._meta_timer.setInterval(2000)
        self._meta_timer.timeout.connect(self._push_meta)

    # ---- meeting lifecycle -------------------------------------------------

    def start_meeting(self, started_at: datetime):
        self._meeting_start = started_at
        self._final_path = None
        self._tags = []
        self._known_names = []
        self._clear_chips()
        self._transcript.setReadOnly(True)
        self._transcript.setPlainText("")
        self._transcript_caption.setText("Live transcript")
        self._save_btn.setVisible(False)
        self._notes_log.setPlainText("")
        self._tag_status.setText("")
        self._speaker_combo.clear()

    def _elapsed(self) -> float:
        if self._meeting_start is None:
            return 0.0
        return max(
            0.0, (datetime.now() - self._meeting_start).total_seconds()
        )

    def update_live(self, text: str):
        """Refresh the streaming transcript without stealing the scrollbar:
        autoscroll only while the user is already pinned to the bottom."""
        if self._final_path is not None:
            return
        bar = self._transcript.verticalScrollBar()
        pinned = bar.value() >= bar.maximum() - 4
        self._transcript.setPlainText(text)
        if pinned:
            bar.setValue(bar.maximum())

    def show_final(self, path, text: str):
        """Swap the pane to the saved transcript: editable, with one rename
        chip per speaker label."""
        self._final_path = Path(path)
        self._transcript_caption.setText(
            f"Final transcript — {self._final_path.name} (editable)"
        )
        self._transcript.setReadOnly(False)
        self._transcript.setPlainText(text)
        self._transcript.verticalScrollBar().setValue(0)
        self._save_btn.setVisible(True)
        self._rebuild_chips(text)

    # ---- attendees + speaker assignment ------------------------------------

    _LAST_ATTENDEES = (
        Path.home() / ".cache" / "aloe-scribe" / "last_attendees.json"
    )

    def _add_attendee(self):
        name = self._attendee_edit.text().strip()
        if name:
            self._register_attendee(name)
        self._attendee_edit.clear()

    def _register_attendee(self, name: str):
        if name.lower() in [n.lower() for n in self._known_names]:
            return
        self._known_names.append(name)
        # Roster chip: clicking it assigns that person as the current
        # speaker — the fastest path once the roster is in.
        chip = QPushButton(name)
        chip.setObjectName("btnSkip")
        chip.setToolTip(f"Click while {name} is talking to assign them")
        chip.clicked.connect(lambda _=False, n=name: self._record_tag(n))
        self._tag_chips.addWidget(chip)
        self._speaker_combo.addItem(name)
        self._schedule_meta_push()

    def _save_last_attendees(self):
        """Accumulate a name history (union across meetings, capped) that
        feeds the attendee field's autocomplete."""
        try:
            import json

            self._LAST_ATTENDEES.parent.mkdir(parents=True, exist_ok=True)
            try:
                history = json.loads(self._LAST_ATTENDEES.read_text())
            except Exception:
                history = []
            merged = list(
                dict.fromkeys(
                    [n for n in history if isinstance(n, str)]
                    + self._known_names
                )
            )[-200:]
            self._LAST_ATTENDEES.write_text(json.dumps(merged))
        except Exception:
            pass

    def _assign_current(self):
        name = self._speaker_combo.currentText().strip()
        if not name:
            return
        # Typing a fresh name in the combo adds them to the roster too.
        self._register_attendee(name)
        self._record_tag(name)

    def _record_tag(self, name: str):
        t = self._elapsed()
        self._tags.append((t, name))
        m, s = divmod(int(t), 60)
        self._tag_status.setText(f"Assigned {name} at {m:02d}:{s:02d}")
        self._save_last_attendees()
        self._schedule_meta_push()

    # ---- notes -------------------------------------------------------------

    def _schedule_meta_push(self):
        self._meta_timer.start()

    def _push_meta(self):
        if self._on_meta_changed:
            try:
                self._on_meta_changed(
                    list(self._tags),
                    self._notes_log.toPlainText(),
                    list(self._known_names),
                )
            except Exception:
                pass

    # ---- final-transcript editing -----------------------------------------

    def _clear_chips(self):
        for row in (self._chips_row, self._tag_chips):
            while row.count():
                item = row.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.deleteLater()

    def _rebuild_chips(self, text: str):
        while self._chips_row.count():
            item = self._chips_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        try:
            import speakers

            quotes = speakers.speaker_quotes(text)
        except Exception:
            quotes = []
        # Biggest talkers first, with line counts — name the monsters, skip
        # the two-line fragments.
        quotes.sort(key=lambda q: -q[2])
        self._current_speakers = [q[0] for q in quotes]
        for label, _q, count in quotes[:14]:
            chip = QPushButton(f"{label} · {count}")
            chip.setObjectName("btnSkip")
            chip.setToolTip(f"Rename {label} everywhere in this transcript")
            chip.clicked.connect(lambda _=False, l=label: self._rename_label(l))
            self._chips_row.addWidget(chip)
        self._chips_row.addStretch(1)

    def _rename_label(self, label: str):
        # Existing speakers plus the attendee roster in the dropdown:
        # merging a duplicate cluster or naming a fragment is a pick, not a
        # retype — and silent-so-far attendees are offered too.
        options = [
            n for n in getattr(self, "_current_speakers", [])
            if n != label
        ]
        for n in self._known_names:
            if n != label and n.lower() not in [o.lower() for o in options]:
                options.append(n)
        name, ok = QInputDialog.getItem(
            self,
            "Rename speaker",
            f"Who is {label}? Pick an existing name to merge, or type a new one.",
            options,
            0,
            True,  # editable — free typing still allowed
        )
        name = (name or "").strip()
        if not ok or not name:
            return
        try:
            import speakers

            renamed = speakers.apply_speaker_names(
                self._transcript.toPlainText(), {label: name}
            )
            self._transcript.setPlainText(renamed)
            self._rebuild_chips(renamed)
        except Exception as e:
            QMessageBox.warning(self, "Aloe Scribe", f"Rename failed: {e}")

    def _save_final(self):
        if self._final_path is None or not self._on_save:
            return
        try:
            self._on_save(self._final_path, self._transcript.toPlainText())
            self._transcript_caption.setText(
                f"Final transcript — {self._final_path.name} (saved)"
            )
        except Exception as e:
            QMessageBox.warning(self, "Aloe Scribe", f"Save failed: {e}")


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
        on_name_speakers: Callable = None,
        on_meta_changed: Callable = None,
        on_save_transcript: Callable = None,
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
        self._on_name_speakers = on_name_speakers
        self._on_meta_changed = on_meta_changed
        self._on_save_transcript = on_save_transcript
        self._notes_window: Optional[NotesWindow] = None
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
        self._signals.live_preview_set.connect(self._on_live_preview_set)
        self._signals.live_preview_clear.connect(self._on_live_preview_clear)
        self._signals.live_preview_status.connect(self._on_live_preview_status)
        self._signals.show_error.connect(self._on_show_error)
        self._signals.prompt_speaker_names.connect(self._on_prompt_speaker_names)
        self._signals.processing_status.connect(self._on_processing_status)
        self._signals.processing_draft.connect(self._on_processing_draft)
        self._signals.notes_final.connect(self._on_notes_final)

        # Timer
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick_timer)

        # Window setup
        self.setWindowTitle("Aloe Scribe")
        self.setFixedSize(344, 500)
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

        title = QLabel("Aloe")
        title.setObjectName("appTitle")
        sub = QLabel("Scribe")
        sub.setObjectName("appSub")

        header.addWidget(title)
        header.addSpacing(5)
        header.addWidget(sub)
        header.addStretch()

        self._status_label = QLabel("● Idle")
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

        mic_label = QLabel("Mic level")
        mic_label.setObjectName("deviceLabel")
        self._content_layout.addWidget(mic_label)

        self._mic_level_bar = QProgressBar()
        self._mic_level_bar.setRange(0, 1000)
        self._mic_level_bar.setValue(0)
        self._mic_level_bar.setTextVisible(False)
        self._mic_level_bar.setFixedHeight(8)
        self._content_layout.addWidget(self._mic_level_bar)

        if self._system_on():
            sys_label = QLabel("System audio level")
            sys_label.setObjectName("deviceLabel")
            self._content_layout.addWidget(sys_label)

            self._sys_level_bar = QProgressBar()
            self._sys_level_bar.setRange(0, 1000)
            self._sys_level_bar.setValue(0)
            self._sys_level_bar.setTextVisible(False)
            self._sys_level_bar.setFixedHeight(8)
            self._content_layout.addWidget(self._sys_level_bar)
        else:
            self._sys_level_bar = None

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

        sub = QLabel("Pick your sources, then start capturing.")
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

        # "Transcribe a file…" button: opens a picker so any audio file can
        # be transcribed (recovery of failed recordings, or external files).
        has_transcribe = self._build_transcribe_file_row()
        base = 586 if self._system_on() else 536  # includes the law fine-print line  # +50 for the system-audio meter
        self.setFixedSize(344, base + (56 if has_transcribe else 0))

        btn = QPushButton("Start recording")
        btn.setObjectName("btnStart")
        btn.clicked.connect(self._on_manual_start)
        # Brief debounce: disable the Start button for 600 ms so a residual
        # click from the previous screen (Done → idle) doesn't immediately
        # fire a new recording at the same on-screen coordinate.
        btn.setEnabled(False)
        QTimer.singleShot(600, lambda b=btn: b.setEnabled(True))
        self._content_layout.addWidget(btn)

        # Fine print — recording/transcription law reminder.
        legal = QLabel(
            "Check state & federal recording laws and disclose transcription "
            "to attendees in all-party states."
        )
        legal.setObjectName("finePrint")
        legal.setWordWrap(True)
        legal.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(legal)

        self._update_status("● Idle", "statusIdle")
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
        mic_label = QLabel("Microphone")
        mic_label.setObjectName("deviceLabel")
        self._content_layout.addWidget(mic_label)

        # No auto-detect: it guessed wrong too often (recording a meeting on
        # the built-in mic while a headset sat unused). The dropdown starts on
        # a placeholder that Start refuses, UNLESS the saved mic is actually
        # present right now — then it is preselected, so the everyday
        # same-desk case stays zero-click while a changed environment (other
        # office, missing headset) forces a conscious choice.
        mic_combo = QComboBox()
        mic_combo.addItem("Select microphone…", "")
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
        # Start's gate reads the visible selection: placeholder = no start.
        # (The saved config value is deliberately kept, so plugging the usual
        # headset back in preselects it again next time.)
        self._mic_combo = mic_combo

        # System audio dropdown — explicit on/off rather than the legacy
        # "Auto-detect / BlackHole" device picker. SCK always captures the
        # full desktop mix when on; off is for in-person meetings where the
        # mic already picks up everyone in the room.
        sys_label = QLabel("System audio")
        sys_label.setObjectName("deviceLabel")
        self._content_layout.addWidget(sys_label)

        sys_combo = QComboBox()
        sys_options = [
            ("system", "On, capture system audio"),
            ("off", "Off, mic only (in-person)"),
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

        label = QLabel("Save transcripts to")
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
        """Show a 'Transcribe a file' button that opens a native file picker,
        so ANY audio file can be transcribed — not just orphans the app finds
        in the output folder. Returns True if the row was added."""
        if not self._on_transcribe_file:
            return False

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        self._content_layout.addWidget(sep)

        go = QPushButton("Transcribe a file…")
        go.setObjectName("btnSkip")
        go.clicked.connect(self._on_transcribe_clicked)
        self._content_layout.addWidget(go)
        return True

    def _on_transcribe_clicked(self):
        if not self._on_transcribe_file:
            return
        out_dir = (
            Path(self._output_dir).expanduser()
            if self._output_dir
            else Path("~/meetings").expanduser()
        )
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Choose a recording to transcribe",
            str(out_dir),
            "Audio files (*.wav *.m4a *.mp3 *.aiff *.flac *.ogg);;All files (*)",
        )
        if not path:
            return
        log.info(f"UI: transcribe existing recording: {path}")
        # Show the processing screen, then run transcription off the UI thread.
        self._signals.set_processing.emit()
        import threading

        threading.Thread(
            target=lambda: self._on_transcribe_file(path), daemon=True
        ).start()

    def _render_recording(self, meeting):
        self._state = "recording"
        self._current_meeting = meeting
        self._clear_content()

        # The notes window is the naming mechanism (tag speakers while you
        # can hear them), so it opens with every recording. Closing it keeps
        # it closed for this meeting; the next recording opens it again.
        self._show_notes(start_meeting=True)

        state = QLabel("Recording")
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
            prev_label = QLabel("LIVE TRANSCRIPT · updates as you record")
            prev_label.setObjectName("deviceLabel")
            self._content_layout.addWidget(prev_label)

            self._live_preview_box = QTextEdit()
            self._live_preview_box.setReadOnly(True)
            self._live_preview_box.setFixedHeight(70)
            self._live_preview_box.setPlaceholderText(
                "STT model is starting. The transcript will show within 30 seconds."
            )
            self._content_layout.addWidget(self._live_preview_box)

        stop = QPushButton("Stop & transcribe")
        stop.setObjectName("btnStop")
        stop.clicked.connect(self._on_stop)
        self._content_layout.addWidget(stop)

        notes_btn = QPushButton("Notes && speaker tags")
        notes_btn.setObjectName("btnSkip")
        notes_btn.clicked.connect(lambda: self._show_notes())
        self._content_layout.addWidget(notes_btn)

        self.setFixedSize(344, 640 if self._live_preview_enabled else 525)
        self._update_status("● Recording", "statusRecord")
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

    def _on_live_preview_set(self, text: str):
        # Streaming sends the full, growing transcript each tick — replace and
        # keep the newest line in view. The notes window gets the same feed
        # but with scroll-position-respecting updates.
        try:
            self.notes_update_live(text)
        except Exception:
            pass
        box = self._live_preview_box
        if box is None:
            return
        try:
            box.setPlainText(text)
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
        self._processing_draft_box = None  # widgets were just torn down

        stopped = QLabel("Recording stopped")
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

        # Live stage line ("Transcribing the remote audio (2/2)…") so a
        # multi-minute labeling pass never reads as a frozen app.
        self._processing_status_label = QLabel("Preparing…")
        self._processing_status_label.setObjectName("stateLabel")
        self._processing_status_label.setStyleSheet("font-size: 10px;")
        self._processing_status_label.setWordWrap(True)
        self._processing_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(self._processing_status_label)

        hint = QLabel("Long recordings may take several minutes.")
        hint.setObjectName("stateLabel")
        # Smaller font + tighter letter-spacing so the line isn't clipped at the
        # window edges; word-wrap as a safety net on narrow widths.
        hint.setStyleSheet("font-size: 9px; letter-spacing: 1px;")
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(hint)

        # Back-to-back meetings: recording does not contend with
        # transcription, so the next call can start now. This transcript
        # keeps processing in the background and announces itself with a
        # notification when it lands.
        next_btn = QPushButton("Start next recording")
        next_btn.setObjectName("btnStart")
        next_btn.clicked.connect(self._on_manual_start)
        # Same 600 ms debounce as the idle screen — this button appears at
        # the same coordinates the user just clicked Stop on.
        next_btn.setEnabled(False)
        QTimer.singleShot(600, lambda b=next_btn: b.setEnabled(True))
        self._content_layout.addWidget(next_btn)

        self._processing_timer_label = QLabel("00:00")
        self._processing_timer_label.setObjectName("timer")
        self._processing_timer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._content_layout.addWidget(self._processing_timer_label)

        self._update_status("● Processing", "statusProcess")

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

        state = QLabel("Complete")
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

        # Re-open the speaker-naming dialog for this transcript — for names
        # skipped the first time, or fixing a mistyped one.
        if self._on_name_speakers and hasattr(output_path, "name"):
            name_btn = QPushButton("Name speakers…")
            name_btn.setObjectName("btnSkip")
            name_btn.clicked.connect(
                lambda _=False, p=output_path: threading.Thread(
                    target=lambda: self._on_name_speakers(p), daemon=True
                ).start()
            )
            self._content_layout.addWidget(name_btn)

        self._update_status("● Done", "statusDone")

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

    def live_preview_set(self, text):
        self._signals.live_preview_set.emit(text)

    def live_preview_clear(self):
        self._signals.live_preview_clear.emit()

    def live_preview_status(self, msg):
        self._signals.live_preview_status.emit(msg)

    def show_error(self, msg):
        self._signals.show_error.emit(msg)

    def _on_show_error(self, msg: str):
        # Back to idle first, then surface the failure visibly — errors used
        # to go only to the log file, which read as a frozen Transcribe button.
        self._render_idle()
        QMessageBox.warning(self, "Aloe Scribe", msg)

    def prompt_speaker_names(self, quotes, transcript_text, apply_callback):
        self._signals.prompt_speaker_names.emit(
            (quotes, transcript_text, apply_callback)
        )

    def processing_status(self, msg):
        self._signals.processing_status.emit(msg)

    def _on_processing_status(self, msg: str):
        label = getattr(self, "_processing_status_label", None)
        if label is not None and self._state == "processing":
            try:
                label.setText(msg)
            except RuntimeError:
                pass  # label was torn down by a state change

    def processing_draft(self, text):
        self._signals.processing_draft.emit(text)

    def notes_show_final(self, path, text):
        self._signals.notes_final.emit((path, text))

    def _on_notes_final(self, payload):
        # Only refresh a notes window the user actually has open — never
        # conjure one at transcript-completion time.
        if self._notes_window is not None and self._notes_window.isVisible():
            path, text = payload
            self._notes_window.show_final(path, text)

    def _show_notes(self, start_meeting: bool = False):
        if self._notes_window is None:
            self._notes_window = NotesWindow(
                on_meta_changed=self._on_meta_changed,
                on_save=self._on_save_transcript,
            )
        if start_meeting:
            self._notes_window.start_meeting(datetime.now())
        self._notes_window.show()
        self._notes_window.raise_()

    def notes_update_live(self, text: str):
        if self._notes_window is not None and self._notes_window.isVisible():
            self._notes_window.update_live(text)

    def _on_processing_draft(self, text: str):
        """Show the streamed draft transcript on the processing screen. The
        live stream already holds the full text of the call at Stop, so the
        user sees their meeting immediately while labels and real timestamps
        finalize in the background."""
        if self._state != "processing" or not text:
            return
        box = getattr(self, "_processing_draft_box", None)
        try:
            if box is None:
                caption = QLabel("Draft transcript (speaker labels being finalized):")
                caption.setObjectName("deviceLabel")
                self._content_layout.addWidget(caption)
                box = QTextEdit()
                box.setReadOnly(True)
                box.setMinimumHeight(140)
                self._content_layout.addWidget(box)
                self._processing_draft_box = box
                # Grow the fixed-size window to fit the draft box.
                self.setFixedSize(self.width(), self.height() + 190)
            box.setPlainText(text)
            box.verticalScrollBar().setValue(box.verticalScrollBar().maximum())
        except RuntimeError:
            self._processing_draft_box = None

    def _on_prompt_speaker_names(self, payload):
        """Post-recording dialog: one row per identified speaker with a few
        of their quotes and a name box, plus the FULL transcript below —
        quotes alone are often not enough to recognize a voice, the
        surrounding conversation usually is. Typed names replace labels in
        the saved transcript; blanks keep the label; the same name in two
        boxes merges those speakers (case-insensitive). Skipping changes
        nothing."""
        quotes, transcript_text, apply_callback = payload
        if not quotes:
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Aloe Scribe — Who was speaking?")
        layout = QVBoxLayout(dlg)

        head = QLabel(
            f"{len(quotes)} unique speaker{'s' if len(quotes) != 1 else ''} "
            "identified in this recording. Type a name to replace each label "
            "(leave blank to keep it). The same name in two boxes merges "
            "those speakers. The full transcript is below for context."
        )
        head.setWordWrap(True)
        layout.addWidget(head)

        rows_host = QWidget()
        rows_layout = QVBoxLayout(rows_host)
        edits = {}
        for label, speaker_quotes, count in quotes:
            # Older callers may pass a single quote string.
            if isinstance(speaker_quotes, str):
                speaker_quotes = [speaker_quotes]
            quote_html = "<br>".join(f"<i>“{q}”</i>" for q in speaker_quotes)
            who = QLabel(
                f"<b>{label}</b> ({count} line{'s' if count != 1 else ''})"
                f"<br>{quote_html}"
            )
            who.setWordWrap(True)
            rows_layout.addWidget(who)
            edit = QLineEdit()
            edit.setPlaceholderText("Name")
            rows_layout.addWidget(edit)
            edits[label] = edit

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(rows_host)
        scroll.setMinimumSize(560, min(340, 110 * len(quotes) + 40))
        layout.addWidget(scroll, 2)

        if transcript_text:
            body = QTextEdit()
            body.setReadOnly(True)
            body.setPlainText(transcript_text)
            body.setMinimumHeight(220)
            layout.addWidget(body, 3)

        buttons = QDialogButtonBox()
        save = buttons.addButton(
            "Save names", QDialogButtonBox.ButtonRole.AcceptRole
        )
        buttons.addButton("Skip", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        save.setDefault(True)
        layout.addWidget(buttons)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            mapping = {
                label: edit.text().strip()
                for label, edit in edits.items()
                if edit.text().strip()
            }
            if mapping:
                import threading

                threading.Thread(
                    target=lambda: apply_callback(mapping), daemon=True
                ).start()

    # ------------------------------------------------------------------ #
    # Handlers                                                             #
    # ------------------------------------------------------------------ #

    def _on_start(self, meeting):
        log.info(f"UI: starting recording for '{meeting.title}'")
        self._signals.set_recording.emit(meeting)
        # Start recording — this is fast (just spawns ffmpeg), safe on main thread
        self.on_start_recording(meeting)

    def _on_manual_start(self):
        # Force an explicit mic choice — auto-detect recorded meetings on the
        # wrong microphone when the usual headset wasn't around.
        combo = getattr(self, "_mic_combo", None)
        try:
            if combo is not None and not combo.currentData():
                QMessageBox.information(
                    self,
                    "Aloe Scribe",
                    "Choose your microphone first — the dropdown is still on "
                    "“Select microphone…”.",
                )
                return
        except RuntimeError:
            pass  # combo from a previous screen was torn down; proceed
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
                 on_name_speakers=None, processing_check=None,
                 on_meta_changed=None, on_save_transcript=None,
                 live_preview=False,
                 current_mic="", current_system="", current_output_dir=""):
        self._on_start_recording = on_start_recording
        self._on_stop_recording = on_stop_recording
        self._on_quit = on_quit
        self._list_sources = list_sources
        self._on_device_change = on_device_change
        self._on_output_dir_change = on_output_dir_change
        self._on_transcribe_file = on_transcribe_file
        self._on_name_speakers = on_name_speakers
        self._processing_check = processing_check
        self._on_meta_changed = on_meta_changed
        self._on_save_transcript = on_save_transcript
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
            on_name_speakers=self._on_name_speakers,
            on_meta_changed=self._on_meta_changed,
            on_save_transcript=self._on_save_transcript,
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
            "idle": "Aloe Scribe",
            "recording": "Recording",
            "processing": "Transcribing",
            "done": "Transcript saved",
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
        # Quitting mid-transcription kills the job with it (the interpreter
        # tears down the inference executor: "cannot schedule new futures
        # after shutdown"). Make it a conscious choice — this has silently
        # eaten a meeting's transcript before.
        try:
            if self._processing_check and self._processing_check() and self._window:
                resp = QMessageBox.question(
                    self._window,
                    "Aloe Scribe",
                    "A transcript is still being created and quitting now "
                    "cancels it.\n\nThe recording is kept either way — you "
                    "can transcribe it later with “Transcribe a file…”.\n\n"
                    "Quit anyway?",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                if resp != QMessageBox.StandardButton.Yes:
                    return
        except Exception:
            pass
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
                "Aloe Scribe",
                f"Transcript saved. {output_path.name}",
            )

    def set_idle(self):
        if self._window:
            self._window.set_idle()

    def live_preview_append(self, text):
        if self._window:
            self._window.live_preview_append(text)

    def live_preview_set(self, text):
        if self._window:
            self._window.live_preview_set(text)

    def live_preview_clear(self):
        if self._window:
            self._window.live_preview_clear()

    def live_preview_status(self, msg):
        if self._window:
            self._window.live_preview_status(msg)

    def show_error(self, msg):
        if self._window:
            self._window.show_error(msg)

    def prompt_speaker_names(self, quotes, transcript_text, apply_callback):
        if self._window:
            self._window.prompt_speaker_names(quotes, transcript_text, apply_callback)

    def processing_status(self, msg):
        if self._window:
            self._window.processing_status(msg)

    def processing_draft(self, text):
        if self._window:
            self._window.processing_draft(text)

    def notes_show_final(self, path, text):
        if self._window:
            self._window.notes_show_final(path, text)
