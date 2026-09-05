"""First-run model setup for DMG-only installs (macOS).

The signed DMG carries the app and its whole Python dependency set, but
the model weights are too large to bundle (GitHub caps release assets at
2 GiB, and notarization uploads would crawl). On a machine that never ran
the install script, the app has no models, so this module downloads them
from the repo's model releases into Application Support with a progress
dialog, verifies every checksum, and reassembles the split weights.

Everything is offline-first after this one download: the same GitHub
release URLs the install script uses, no Hugging Face, no telemetry.
"""

import hashlib
import logging
import shutil
import sys
import threading
import urllib.request
from pathlib import Path

log = logging.getLogger("aloe-scribe.bootstrap")

REPO = "vinnybarbs/Aloe-Scribe"

# name -> (release tag, human label, approx size, needed for)
MODELS = {
    "parakeet-tdt-0.6b-v3": (
        "model-parakeet-tdt-0.6b-v3",
        "Speech recognition (Parakeet)",
        "2.4 GB",
        "transcription",
    ),
    "qwen3.5-4b-mlx-4bit": (
        "model-qwen3.5-4b-mlx-4bit",
        "Meeting summaries (Qwen, runs locally)",
        "3.1 GB",
        "summaries",
    ),
}


def app_support_models() -> Path:
    return (
        Path.home() / "Library" / "Application Support" / "Aloe Scribe"
        / "models"
    )


def is_present(name: str) -> bool:
    """The model exists in any location the resolvers look at."""
    candidates = [
        app_support_models() / name,
        Path.home() / "aloe-scribe" / "models" / name,
    ]
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "models" / name)
    else:
        candidates.append(
            Path(__file__).resolve().parent.parent / "models" / name
        )
    for c in candidates:
        try:
            if (c / "model.safetensors").is_file() or any(
                c.glob("model*.safetensors")
            ):
                return True
        except OSError:
            continue
    return False


def _fetch(url: str, dest: Path, progress, expected_sha: str = ""):
    """Stream one file with progress callbacks and an optional hash check.
    progress(bytes_this_file, total_this_file_or_0)."""
    req = urllib.request.Request(url, headers={"User-Agent": "AloeScribe"})
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as r:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                progress(done, total)
    if expected_sha and h.hexdigest() != expected_sha:
        raise ValueError(f"checksum mismatch for {dest.name}")


def download_model(name: str, status_cb) -> None:
    """Download one model release into Application Support.

    status_cb(text, frac) with frac in [0,1] or None for indeterminate.
    Raises on any failure; a partial download never lands in the final
    location (work happens in a .tmp dir renamed at the end)."""
    tag = MODELS[name][0]
    base = f"https://github.com/{REPO}/releases/download/{tag}"
    dest = app_support_models() / name
    work = app_support_models() / f".tmp-{name}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    status_cb(f"Fetching manifest for {name}…", None)
    sums = work / "SHA256SUMS"
    _fetch(f"{base}/SHA256SUMS", sums, lambda d, t: None)
    manifest = {}   # filename -> sha
    full_sha = ""
    for line in sums.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        sha, fname = parts
        if fname == "model.safetensors":
            full_sha = sha       # hash of the reassembled file
        else:
            manifest[fname] = sha

    files = sorted(manifest)
    for i, fname in enumerate(files):
        def prog(done, total, _i=i, _n=len(files), _f=fname):
            frac = (_i + (done / total if total else 0)) / _n
            mb = done // (1024 * 1024)
            status_cb(f"Downloading {_f} ({mb} MB)…", frac)
        _fetch(f"{base}/{fname}", work / fname, prog, manifest[fname])

    parts = sorted(work.glob("model.safetensors.part-*"))
    if parts:
        status_cb("Assembling model weights…", None)
        h = hashlib.sha256()
        with open(work / "model.safetensors", "wb") as out:
            for p in parts:
                with open(p, "rb") as f:
                    while True:
                        chunk = f.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        h.update(chunk)
                p.unlink()
        if full_sha and h.hexdigest() != full_sha:
            raise ValueError("reassembled weights failed the checksum")
    sums.unlink()

    if dest.exists():
        shutil.rmtree(dest)
    work.rename(dest)
    status_cb(f"{name} ready.", 1.0)


# ---------------------------------------------------------------------------
# Qt first-run dialog (macOS frozen app)
# ---------------------------------------------------------------------------

def ensure_models() -> None:
    """Gate the frozen app on its models. Called before the service starts.

    If the speech model is present, returns immediately. Otherwise shows a
    one-time setup dialog that downloads it (and, if the box stays checked,
    the summary model) with progress. Quitting instead exits the app —
    without the speech model there is nothing the app can do."""
    if is_present("parakeet-tdt-0.6b-v3"):
        return

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QDialog, QHBoxLayout, QLabel,
        QProgressBar, QPushButton, QVBoxLayout,
    )

    app = QApplication.instance() or QApplication(sys.argv)

    dlg = QDialog()
    dlg.setWindowTitle("Aloe Scribe setup")
    dlg.setMinimumWidth(460)
    lay = QVBoxLayout(dlg)
    head = QLabel(
        "<b>One-time setup</b><br>Aloe Scribe runs entirely on this Mac, "
        "so it needs its AI models downloaded once. They come from the "
        "project's own GitHub releases and are checksum-verified."
    )
    head.setWordWrap(True)
    lay.addWidget(head)
    want_summaries = QCheckBox(
        "Also download the meeting summary model (3.1 GB)"
    )
    want_summaries.setChecked(True)
    lay.addWidget(QLabel("Speech recognition model: 2.4 GB (required)"))
    lay.addWidget(want_summaries)
    status = QLabel("")
    status.setWordWrap(True)
    lay.addWidget(status)
    bar = QProgressBar()
    bar.setRange(0, 1000)
    bar.hide()
    lay.addWidget(bar)
    row = QHBoxLayout()
    quit_btn = QPushButton("Quit")
    go_btn = QPushButton("Download")
    go_btn.setDefault(True)
    row.addStretch(1)
    row.addWidget(quit_btn)
    row.addWidget(go_btn)
    lay.addLayout(row)

    state = {"text": "", "frac": None, "done": False, "error": None}

    def worker(with_summaries: bool):
        def cb(text, frac):
            state["text"], state["frac"] = text, frac
        try:
            names = ["parakeet-tdt-0.6b-v3"]
            if with_summaries:
                names.append("qwen3.5-4b-mlx-4bit")
            for n, name in enumerate(names):
                def scoped(text, frac, _n=n, _total=len(names)):
                    overall = (
                        None if frac is None else (_n + frac) / _total
                    )
                    cb(f"[{_n + 1}/{_total}] {text}", overall)
                download_model(name, scoped)
            state["done"] = True
        except Exception as e:
            log.exception("Model download failed")
            state["error"] = str(e)

    def start():
        go_btn.setEnabled(False)
        want_summaries.setEnabled(False)
        bar.show()
        threading.Thread(
            target=worker, args=(want_summaries.isChecked(),), daemon=True
        ).start()

    def tick():
        if state["error"]:
            status.setText(
                "Download failed: " + state["error"]
                + "\nCheck your connection and click Download to retry."
            )
            bar.hide()
            state["error"] = None
            go_btn.setEnabled(True)
            want_summaries.setEnabled(True)
            return
        if state["text"]:
            status.setText(state["text"])
        if state["frac"] is None:
            bar.setRange(0, 0)
        else:
            bar.setRange(0, 1000)
            bar.setValue(int(state["frac"] * 1000))
        if state["done"]:
            dlg.accept()

    timer = QTimer(dlg)
    timer.timeout.connect(tick)
    timer.start(200)
    go_btn.clicked.connect(start)
    quit_btn.clicked.connect(dlg.reject)
    dlg.setWindowFlag(Qt.WindowStaysOnTopHint, True)

    if dlg.exec() != QDialog.Accepted:
        sys.exit(0)
