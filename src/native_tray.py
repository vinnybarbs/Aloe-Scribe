"""
native_tray.py — macOS menu-bar status item via NSStatusItem (PyObjC).

Qt's QSystemTrayIcon silently fails to draw a visible status item on
recent macOS (Tahoe/Sequoia) even when isVisible() reports True. PyObjC's
NSStatusItem path is the supported, reliable way to put an icon in the
menu bar, so we use it directly for this single piece of UX.

Public API:
    tray = NativeTray(icon_path, on_show, on_open_folder, on_quit, on_toggle)
    tray.set_status_text("Recording…")
    tray.set_icon(icon_path)
"""

import logging
import objc
from pathlib import Path
from typing import Callable, Optional

from AppKit import NSStatusBar, NSImage, NSMenu, NSMenuItem
from Foundation import NSObject

log = logging.getLogger(__name__)

# Menu-bar status items want their image around 22pt logical / 44px @2x.
_MENU_BAR_ICON_PT = 22


class _TrayActionTarget(NSObject):
    """An NSObject that holds Python callbacks for menu actions."""

    # PyObjC requires init() to be exposed as an Objective-C selector.
    def init(self):
        self = objc.super(_TrayActionTarget, self).init()
        if self is None:
            return None
        self._on_show = None
        self._on_open_folder = None
        self._on_quit = None
        self._on_toggle = None
        return self

    @objc.python_method
    def configure(self, on_show, on_open_folder, on_quit, on_toggle):
        self._on_show = on_show
        self._on_open_folder = on_open_folder
        self._on_quit = on_quit
        self._on_toggle = on_toggle

    # Action selectors (Objective-C signatures end with `:`)
    def show_(self, sender):
        if self._on_show:
            self._on_show()

    def openFolder_(self, sender):
        if self._on_open_folder:
            self._on_open_folder()

    def quit_(self, sender):
        if self._on_quit:
            self._on_quit()

    def toggle_(self, sender):
        if self._on_toggle:
            self._on_toggle()


class NativeTray:
    def __init__(
        self,
        icon_path: Path,
        on_show: Callable,
        on_open_folder: Callable,
        on_quit: Callable,
        on_toggle: Optional[Callable] = None,
    ):
        self._icon_path = Path(icon_path)
        self._status_text = "Aloe Scribe — Idle"

        # Strong references — without these PyObjC garbage-collects the
        # status item and the icon vanishes a beat later.
        self._target = _TrayActionTarget.alloc().init()
        self._target.configure(on_show, on_open_folder, on_quit, on_toggle)

        self._bar = NSStatusBar.systemStatusBar()
        self._item = self._bar.statusItemWithLength_(-1.0)   # NSVariableStatusItemLength

        self._apply_icon(self._icon_path)
        self._item.button().setToolTip_("Aloe Scribe")
        if on_toggle is not None:
            # Single click on the icon toggles the window. Right-click /
            # long-press still opens the menu (we attach it below).
            self._item.button().setTarget_(self._target)
            self._item.button().setAction_("toggle:")

        self._rebuild_menu()
        log.info("NativeTray: status item attached to menu bar")

    def _apply_icon(self, path: Path):
        if not path.exists():
            log.warning(f"NativeTray: icon not found at {path}")
            self._item.button().setTitle_("🌿")
            return
        image = NSImage.alloc().initWithContentsOfFile_(str(path))
        if image is None:
            log.warning(f"NativeTray: NSImage failed to load {path}")
            self._item.button().setTitle_("🌿")
            return
        image.setSize_((_MENU_BAR_ICON_PT, _MENU_BAR_ICON_PT))
        # We keep the colored leaf rather than tinting it as a template;
        # the user explicitly wants the green visible at a glance.
        image.setTemplate_(False)
        self._item.button().setImage_(image)
        # Belt-and-braces fallback: if for some reason macOS doesn't render
        # the image (notch overflow, weird Tahoe filtering), the emoji is
        # still visible. setImage + setTitle both showing is fine.
        self._item.button().setTitle_("🌿")

    def _rebuild_menu(self):
        menu = NSMenu.alloc().init()

        header = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            self._status_text, None, ""
        )
        header.setEnabled_(False)
        menu.addItem_(header)
        menu.addItem_(NSMenuItem.separatorItem())

        show = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Show Window", "show:", ""
        )
        show.setTarget_(self._target)
        menu.addItem_(show)

        folder = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Open Transcripts Folder", "openFolder:", ""
        )
        folder.setTarget_(self._target)
        menu.addItem_(folder)

        menu.addItem_(NSMenuItem.separatorItem())

        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit Aloe Scribe", "quit:", "q"
        )
        quit_item.setTarget_(self._target)
        menu.addItem_(quit_item)

        # If a single-click action is wired up on the button, the menu must
        # be attached via setMenu only for right-click / long-press. Setting
        # both is fine — macOS does the right thing.
        self._item.setMenu_(menu)
        self._menu = menu  # strong ref

    def set_status_text(self, text: str):
        if text == self._status_text:
            return
        self._status_text = text
        self._rebuild_menu()

    def set_icon(self, path: Path):
        self._icon_path = Path(path)
        self._apply_icon(self._icon_path)
