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
        "tomli",
    ],
    "packages": [
        "PyQt6",
        "PIL",
        "objc",
        "AppKit",
        "Foundation",
        # Parakeet TDT backend (Apple Silicon). Pulls in mlx as a transitive
        # dependency — both need to be bundled so the frozen .app can
        # import them without the venv on the user's machine.
        "parakeet_mlx",
        "mlx",
        "huggingface_hub",
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
