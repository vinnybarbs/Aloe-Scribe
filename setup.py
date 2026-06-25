"""
py2app build configuration for Aloe Scribe.
Usage: .venv/bin/python3 setup.py py2app
"""

import sys
sys.path.insert(0, "src")

from setuptools import setup

APP = ["src/main.py"]
DATA_FILES = [
    ("assets", ["assets/icon.png"]),
    ("config", ["config/config.toml"]),
    # Swift helper that captures system audio via ScreenCaptureKit. Ends up
    # at Contents/Resources/bin/aloe-audio-capture inside the .app bundle —
    # recorder_mac._helper_path() looks for it there.
    ("bin", ["bin/aloe-audio-capture"]),
]

OPTIONS = {
    "argv_emulation": False, "iconfile": "assets/AppIcon.icns",
    "includes": [
        "meeting",
        "recorder_mac",
        "recorder",
        "transcriber",
        "transcriber_parakeet",
        "syncer",
        "notifications",
        "ui_mac",
        "ui",
        "native_tray",
        "tomli",
        # Stdlib terminal modules that click (pulled in transitively by the
        # parakeet/huggingface chain) imports LAZILY inside a function, so
        # py2app's static graph never sees them and drops them from the frozen
        # stdlib. Without these the bundled app fails with "No module named
        # 'tty'" the moment it tries to load Parakeet, which looks like
        # "parakeet-mlx not installed." Force them in.
        "tty",
        "termios",
        "pty",
    ],
    "packages": [
        "PyQt6",
        "PIL",
        "objc",
        "AppKit",
        "Foundation",
    ],
    # mlx is a namespace package with native .so files that py2app's
    # modulegraph can't introspect cleanly. parakeet_mlx imports mlx, and
    # huggingface_hub pulls in huge transitive deps that also break the
    # bundle. Exclude all three from the frozen build; main.py prepends the
    # project venv's site-packages to sys.path at startup so they import
    # from there at runtime.
    "excludes": [
        "parakeet_mlx",
        "mlx",
        "huggingface_hub",
        "transformers",
        "torch",
        "tokenizers",
        "safetensors",
    ],
    "plist": {
        "CFBundleName": "Aloe Scribe",
        "CFBundleDisplayName": "Aloe Scribe",
        "CFBundleIdentifier": "com.aloescribe.app",
        "CFBundleVersion": "1.0",
        "CFBundleShortVersionString": "1.0",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": "Aloe Scribe needs microphone access to record meeting audio.",
        "NSScreenCaptureUsageDescription": "Aloe Scribe needs screen capture access to record system audio (the audio from the apps you're listening to) — no video is captured or saved.",
        "LSUIElement": False,
    },
}

setup(
    name="Aloe Scribe",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
