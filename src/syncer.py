"""
syncer.py — Watches the local meetings folder and syncs new files to SharePoint via rclone.

Queues files when offline and retries automatically when connectivity returns.
"""

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional
from queue import Queue, Empty

log = logging.getLogger(__name__)


def _internet_available() -> bool:
    """Quick connectivity check (works on Linux and macOS)."""
    import socket
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=3).close()
        return True
    except OSError:
        return False


class Syncer:
    """
    Accepts file paths and syncs them to a rclone remote.
    Files are queued locally if offline and retried every 60 seconds.
    """

    def __init__(self, rclone_remote: str, enabled: bool = True, notify_on_sync: bool = True):
        self.rclone_remote = rclone_remote  # e.g. "sharepoint:Documents/Meetings"
        self.enabled = enabled
        self.notify_on_sync = notify_on_sync
        self._queue: Queue = Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        if self.enabled:
            self._thread.start()
            log.info("Syncer started")
        else:
            log.info("Sync disabled in config — files saved locally only")

    def stop(self):
        self._stop.set()

    def enqueue(self, path: Path):
        """Add a file to the sync queue."""
        if not self.enabled:
            log.info(f"Sync disabled — file saved locally: {path}")
            return
        log.info(f"Queued for sync: {path.name}")
        self._queue.put(path)

    def _run(self):
        """Background thread: drain the queue, retry on failure."""
        pending: list[Path] = []

        while not self._stop.is_set():
            # Collect any newly queued files
            while True:
                try:
                    path = self._queue.get_nowait()
                    pending.append(path)
                except Empty:
                    break

            if pending and _internet_available():
                still_pending = []
                for path in pending:
                    if self._sync_file(path):
                        if self.notify_on_sync:
                            _notify_sync(path.name)
                    else:
                        still_pending.append(path)
                pending = still_pending
            elif pending:
                log.warning(f"Offline — {len(pending)} file(s) queued, retrying in 60s")

            self._stop.wait(60)

    def _sync_file(self, path: Path) -> bool:
        """Upload a single file to the rclone remote. Returns True on success."""
        if not path.exists():
            log.warning(f"File not found, skipping: {path}")
            return True  # Nothing to retry

        # rclone copyto uploads a single file to a specific remote path
        remote_path = f"{self.rclone_remote}/{path.name}"
        cmd = ["rclone", "copyto", str(path), remote_path, "--progress"]

        log.info(f"Syncing {path.name} → {remote_path}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                log.info(f"Synced: {path.name}")
                return True
            else:
                log.error(f"rclone error: {result.stderr.strip()}")
                return False
        except subprocess.TimeoutExpired:
            log.error("rclone timed out")
            return False
        except FileNotFoundError:
            log.error("rclone not found — install with: sudo apt install rclone")
            return False


def _notify_sync(filename: str):
    """Show a desktop notification for a successful sync."""
    import notifications
    notifications.send("Aloe Scribe", f"Synced to SharePoint: {filename}")
