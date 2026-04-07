"""
ui.py — Aloe Scribe GTK window UI with AppIndicator3 system tray icon.

Shows a persistent aloe leaf icon in the system tray (via AppIndicator3,
which works on GNOME). Clicking the tray icon toggles the floating window.
"""

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("AppIndicator3", "0.1")
from gi.repository import Gtk, GLib, Gdk, AppIndicator3
import os
import subprocess
import threading
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tray icon PNG generator — writes a leaf icon to /tmp for AppIndicator3
# ---------------------------------------------------------------------------
_ICON_DIR = Path("/tmp/aloe-scribe-icons")

def _ensure_tray_icons():
    """Generate colored leaf PNGs that AppIndicator3 can use."""
    _ICON_DIR.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        log.warning("Pillow not installed — tray icon will use fallback")
        return False

    colors = {
        "idle":       "#3A8C5A",
        "notifying":  "#E5A020",
        "recording":  "#C94040",
        "processing": "#3878C8",
    }

    for name, color in colors.items():
        path = _ICON_DIR / f"aloe-{name}.png"
        if path.exists():
            continue
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        leaf = [
            (32, 4), (52, 18), (56, 36), (46, 52),
            (32, 60), (18, 52), (8, 36), (12, 18),
        ]
        d.polygon(leaf, fill=color)
        d.line([(32, 60), (32, 44)], fill="white", width=3)
        d.line([(32, 44), (22, 36)], fill="white", width=2)
        d.line([(32, 44), (42, 36)], fill="white", width=2)
        img.save(str(path))

    return True


class AloeScribeWindow(Gtk.ApplicationWindow):
    def __init__(
        self,
        application: Gtk.Application,
        on_start_recording: Callable,
        on_stop_recording: Callable,
        on_quit: Callable,
    ):
        super().__init__(application=application, title="Aloe Scribe")
        self.on_start_recording = on_start_recording
        self.on_stop_recording = on_stop_recording
        self.on_quit = on_quit

        self._current_meeting = None
        self._state = "idle"
        self._timer_seconds = 0
        self._timer_id = None

        # Set the window/dock icon to the aloe leaf
        _ensure_tray_icons()
        _app_icon = Path(__file__).parent.parent / "assets" / "icon.png"
        if not _app_icon.exists():
            _app_icon = _ICON_DIR / "aloe-idle.png"
        if _app_icon.exists():
            self.set_icon_from_file(str(_app_icon))
            Gtk.Window.set_default_icon_from_file(str(_app_icon))

        # Set WM_CLASS so GNOME matches this to a .desktop entry
        self.set_wmclass("aloe-scribe", "Aloe Scribe")

        # Window setup
        self.set_default_size(300, 200)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_border_width(20)
        self.connect("delete-event", self._on_close)

        # Load CSS
        css = b"""
            window {
                background-color: #ffffff;
            }
            .app-title {
                font-size: 13px;
                font-weight: bold;
                letter-spacing: 2px;
                color: #3A8C5A;
            }
            .app-sub {
                font-size: 11px;
                color: #3A8C5A;
                letter-spacing: 1px;
            }
            .state-label {
                font-size: 10px;
                color: #999999;
                letter-spacing: 2px;
            }
            .meeting-title {
                font-size: 14px;
                font-weight: bold;
                color: #222222;
            }
            .meeting-time {
                font-size: 11px;
                color: #888888;
            }
            .timer {
                font-size: 32px;
                font-weight: bold;
                color: #222222;
                font-family: monospace;
            }
            .btn-start {
                background-color: #3A8C5A;
                color: white;
                border-radius: 6px;
                font-weight: bold;
                font-size: 13px;
                padding: 8px 16px;
                border: none;
            }
            .btn-stop {
                background-color: #C94040;
                color: white;
                border-radius: 6px;
                font-weight: bold;
                font-size: 13px;
                padding: 8px 16px;
                border: none;
            }
            .btn-skip {
                background-color: #f0f0f0;
                color: #555555;
                border-radius: 6px;
                font-size: 13px;
                padding: 8px 16px;
                border: none;
            }
            .status-idle { color: #888888; }
            .status-recording { color: #C94040; }
            .status-processing { color: #3878C8; }
            .status-done { color: #3A8C5A; }
            separator { background-color: #eeeeee; margin-top: 10px; margin-bottom: 10px; }
        """
        style_provider = Gtk.CssProvider()
        style_provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            style_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self._build_ui()
        self._build_tray_icon()
        self.show_all()

    def _build_tray_icon(self):
        """Create an AppIndicator3 tray icon with a right-click menu."""
        has_icons = _ensure_tray_icons()
        if not has_icons:
            log.warning("Tray icons not generated — skipping tray")
            self._indicator = None
            return

        self._indicator = AppIndicator3.Indicator.new(
            "aloe-scribe",
            str(_ICON_DIR / "aloe-idle.png"),
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self._indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self._indicator.set_title("Aloe Scribe")
        self._update_tray_menu()

    def _update_tray_menu(self):
        """Rebuild the right-click menu for the tray icon."""
        if not getattr(self, "_indicator", None):
            return

        menu = Gtk.Menu()

        # Status header
        status_map = {
            "idle": "Aloe Scribe — Idle",
            "notifying": "Meeting soon",
            "recording": "Recording...",
            "processing": "Transcribing...",
            "done": "Done — transcript saved",
        }
        header = Gtk.MenuItem(label=status_map.get(self._state, "Aloe Scribe"))
        header.set_sensitive(False)
        menu.append(header)
        menu.append(Gtk.SeparatorMenuItem())

        # Show/hide window
        toggle = Gtk.MenuItem(label="Hide Window" if self.get_visible() else "Show Window")
        toggle.connect("activate", lambda _: self._toggle_window())
        menu.append(toggle)

        # Open meetings folder
        folder = Gtk.MenuItem(label="Open Meetings Folder")
        folder.connect("activate", lambda _: self._open_folder())
        menu.append(folder)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Quit Aloe Scribe")
        quit_item.connect("activate", lambda _: self._on_close(None, None))
        menu.append(quit_item)

        menu.show_all()
        self._indicator.set_menu(menu)

    def _toggle_window(self):
        if self.get_visible():
            self.hide()
        else:
            self.show_all()
            self.present()

    def _set_tray_icon_state(self, state: str):
        """Update the tray icon color to reflect current state."""
        if not getattr(self, "_indicator", None):
            return
        icon_name = f"aloe-{state}" if state != "done" else "aloe-idle"
        icon_path = _ICON_DIR / f"{icon_name}.png"
        if icon_path.exists():
            self._indicator.set_icon_full(str(icon_path), f"Aloe Scribe — {state}")
        self._update_tray_menu()

    def _build_ui(self):
        self._box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add(self._box)

        # Header
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title = Gtk.Label(label="ALOE")
        title.get_style_context().add_class("app-title")
        sub = Gtk.Label(label="SCRIBE")
        sub.get_style_context().add_class("app-sub")
        header.pack_start(title, False, False, 0)
        header.pack_start(sub, False, False, 0)

        self._status_label = Gtk.Label(label="● IDLE")
        self._status_label.get_style_context().add_class("status-idle")
        header.pack_end(self._status_label, False, False, 0)
        self._box.pack_start(header, False, False, 0)

        sep = Gtk.Separator()
        self._box.pack_start(sep, False, False, 0)

        # Content area (swapped per state)
        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._box.pack_start(self._content, True, True, 0)

        self._render_idle()

    def _clear_content(self):
        for child in self._content.get_children():
            self._content.remove(child)

    # ------------------------------------------------------------------ #
    # State renderers                                                       #
    # ------------------------------------------------------------------ #

    def _render_idle(self):
        self._state = "idle"
        self._clear_content()
        label = Gtk.Label(label="No meetings detected.")
        label.get_style_context().add_class("meeting-time")
        label.set_margin_top(16)
        self._content.pack_start(label, False, False, 0)

        sub = Gtk.Label(label="Watching your calendar...")
        sub.get_style_context().add_class("state-label")
        self._content.pack_start(sub, False, False, 0)

        # Manual start button
        btn = Gtk.Button(label="Start Recording Now")
        btn.get_style_context().add_class("btn-start")
        btn.set_margin_top(16)
        btn.connect("clicked", self._on_manual_start)
        self._content.pack_start(btn, False, False, 0)

        self._update_status("● IDLE", "status-idle")
        self._content.show_all()

    def _render_notify(self, meeting):
        self._state = "notifying"
        self._clear_content()

        state = Gtk.Label(label="MEETING SOON")
        state.get_style_context().add_class("state-label")
        self._content.pack_start(state, False, False, 0)

        title = Gtk.Label(label=meeting.title)
        title.get_style_context().add_class("meeting-title")
        title.set_margin_top(4)
        self._content.pack_start(title, False, False, 0)

        time_label = Gtk.Label(
            label=f"Starts in ~4 minutes · {meeting.start.strftime('%I:%M %p')}"
        )
        time_label.get_style_context().add_class("meeting-time")
        self._content.pack_start(time_label, False, False, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_margin_top(12)

        skip = Gtk.Button(label="Skip")
        skip.get_style_context().add_class("btn-skip")
        skip.connect("clicked", lambda b: self.set_idle())

        start = Gtk.Button(label="Start Recording")
        start.get_style_context().add_class("btn-start")
        start.connect("clicked", lambda b: self._on_start(meeting))

        btn_row.pack_start(skip, True, True, 0)
        btn_row.pack_start(start, True, True, 0)
        self._content.pack_start(btn_row, False, False, 0)

        self._update_status("● MEETING SOON", "status-idle")
        self._content.show_all()

    def _render_recording(self, meeting):
        self._state = "recording"
        self._clear_content()

        state = Gtk.Label(label="RECORDING")
        state.get_style_context().add_class("state-label")
        self._content.pack_start(state, False, False, 0)

        title = Gtk.Label(label=meeting.title if meeting else "Recording")
        title.get_style_context().add_class("meeting-title")
        self._content.pack_start(title, False, False, 0)

        self._timer_label = Gtk.Label(label="00:00")
        self._timer_label.get_style_context().add_class("timer")
        self._timer_label.set_margin_top(8)
        self._content.pack_start(self._timer_label, False, False, 0)

        stop = Gtk.Button(label="Stop & Transcribe")
        stop.get_style_context().add_class("btn-stop")
        stop.set_margin_top(12)
        stop.connect("clicked", lambda b: self._on_stop())
        self._content.pack_start(stop, False, False, 0)

        self._update_status("● RECORDING", "status-recording")
        self._start_timer()
        self._content.show_all()

    def _render_processing(self):
        self._state = "processing"
        self._clear_content()

        state = Gtk.Label(label="TRANSCRIBING")
        state.get_style_context().add_class("state-label")
        self._content.pack_start(state, False, False, 0)

        label = Gtk.Label(label="Running Whisper locally...")
        label.get_style_context().add_class("meeting-time")
        label.set_margin_top(8)
        self._content.pack_start(label, False, False, 0)

        spinner = Gtk.Spinner()
        spinner.set_size_request(32, 32)
        spinner.set_margin_top(12)
        spinner.start()
        self._content.pack_start(spinner, False, False, 0)

        self._update_status("● PROCESSING", "status-processing")
        self._content.show_all()

    def _render_done(self, output_path: Path):
        self._state = "done"
        self._clear_content()

        state = Gtk.Label(label="COMPLETE")
        state.get_style_context().add_class("state-label")
        self._content.pack_start(state, False, False, 0)

        fname = Gtk.Label(label=output_path.name)
        fname.get_style_context().add_class("meeting-time")
        fname.set_margin_top(4)
        self._content.pack_start(fname, False, False, 0)

        sync = Gtk.Label(label="Syncing to SharePoint...")
        sync.get_style_context().add_class("state-label")
        self._content.pack_start(sync, False, False, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_margin_top(12)

        done = Gtk.Button(label="Done")
        done.get_style_context().add_class("btn-skip")
        done.connect("clicked", lambda b: self.set_idle())

        open_btn = Gtk.Button(label="Open Folder")
        open_btn.get_style_context().add_class("btn-start")
        open_btn.connect("clicked", lambda b: self._open_folder())

        btn_row.pack_start(done, True, True, 0)
        btn_row.pack_start(open_btn, True, True, 0)
        self._content.pack_start(btn_row, False, False, 0)

        self._update_status("● DONE", "status-done")
        self._content.show_all()

    # ------------------------------------------------------------------ #
    # Public state setters (thread-safe via GLib.idle_add)                #
    # ------------------------------------------------------------------ #

    def notify_upcoming(self, meeting):
        GLib.idle_add(self._render_notify, meeting)

    def set_recording(self, meeting):
        GLib.idle_add(self._render_recording, meeting)

    def set_processing(self):
        self._stop_timer()
        GLib.idle_add(self._render_processing)

    def set_done(self, output_path: Path):
        GLib.idle_add(self._render_done, output_path)
        GLib.timeout_add_seconds(10, self.set_idle)

    def set_idle(self):
        GLib.idle_add(self._render_idle)
        return False  # stop GLib timeout if called that way

    # ------------------------------------------------------------------ #
    # Handlers                                                             #
    # ------------------------------------------------------------------ #

    def _on_start(self, meeting):
        self.set_recording(meeting)
        self.on_start_recording(meeting)

    def _on_manual_start(self, btn):
        from calendar_watcher import Meeting
        manual = Meeting(
            title="Manual Recording",
            start=datetime.now().astimezone(),
            end=datetime.now().astimezone(),
        )
        self._on_start(manual)

    def _on_stop(self):
        self.set_processing()
        threading.Thread(target=self.on_stop_recording, daemon=True).start()

    def _on_close(self, widget, event):
        # If called from tray Quit menu (widget is None), actually quit
        if widget is None:
            self.on_quit()
            Gtk.main_quit()
            return False
        # If window X button pressed, hide to tray instead of quitting
        if getattr(self, "_indicator", None):
            self.hide()
            return True  # prevent destroy
        # No tray — actually quit
        self.on_quit()
        Gtk.main_quit()
        return False

    def _open_folder(self):
        import subprocess
        subprocess.Popen(["xdg-open", str(Path("~/meetings").expanduser())])

    # ------------------------------------------------------------------ #
    # Timer                                                                #
    # ------------------------------------------------------------------ #

    def _start_timer(self):
        self._timer_seconds = 0
        self._timer_id = GLib.timeout_add(1000, self._tick_timer)

    def _tick_timer(self):
        self._timer_seconds += 1
        m = self._timer_seconds // 60
        s = self._timer_seconds % 60
        if hasattr(self, "_timer_label"):
            self._timer_label.set_text(f"{m:02d}:{s:02d}")
        return True  # keep ticking

    def _stop_timer(self):
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = None

    def _update_status(self, text: str, css_class: str):
        ctx = self._status_label.get_style_context()
        for cls in ["status-idle", "status-recording", "status-processing", "status-done"]:
            ctx.remove_class(cls)
        ctx.add_class(css_class)
        self._status_label.set_text(text)
        # Sync tray icon state
        state = self._state
        self._set_tray_icon_state(state)

    # ------------------------------------------------------------------ #
    # Run                                                                  #
    # ------------------------------------------------------------------ #

    def run(self):
        # Managed by AloeScribeApp — should not be called directly
        pass

    def stop(self):
        GLib.idle_add(self._app.quit if hasattr(self, "_app") else Gtk.main_quit)


class AloeScribeApp(Gtk.Application):
    """
    Wraps AloeScribeWindow in a GtkApplication so that:
    - Clicking the dock icon re-presents the window (activate signal)
    - Only one instance runs per session (application_id uniqueness)
    """

    def __init__(self, on_start_recording, on_stop_recording, on_quit):
        super().__init__(application_id="com.aloescribe.app")
        self._on_start_recording = on_start_recording
        self._on_stop_recording = on_stop_recording
        self._on_quit = on_quit
        self._window = None

    def do_activate(self):
        """Called on first launch AND every subsequent dock icon click."""
        if self._window is None:
            self._window = AloeScribeWindow(
                application=self,
                on_start_recording=self._on_start_recording,
                on_stop_recording=self._on_stop_recording,
                on_quit=self._on_quit,
            )
            self._window._app = self
        else:
            # Dock icon clicked again — show and raise the window
            self._window.show_all()
            self._window.present()

    def run(self):
        super().run(None)

    # Proxy state methods so main.py can call them on the app object
    def notify_upcoming(self, meeting):
        if self._window:
            self._window.notify_upcoming(meeting)
            import notifications
            notifications.send("Aloe Scribe", f"Meeting in ~4 min: {meeting.title}")

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
            notifications.send("Aloe Scribe — Done", f"Transcript saved: {output_path.name}")

    def set_idle(self):
        if self._window:
            self._window.set_idle()
