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
]

OPTIONS = {
    "argv_emulation": False,
    "includes": [
        "calendar_watcher",
        "recorder_mac",
        "recorder",
        "transcriber",
        "syncer",
        "notifications",
        "ui_mac",
        "ui",
        "tomli",
    ],
    "packages": [
        "PyQt6",
        "icalendar",
        "requests",
        "certifi",
        "PIL",
        "objc",
        "AppKit",
        "Foundation",
    ],
    "plist": {
        "CFBundleName": "Aloe Scribe",
        "CFBundleDisplayName": "Aloe Scribe",
        "CFBundleIdentifier": "com.aloescribe.app",
        "CFBundleVersion": "1.0",
        "CFBundleShortVersionString": "1.0",
        "NSHighResolutionCapable": True,
        "NSMicrophoneUsageDescription": "Aloe Scribe needs microphone access to record meeting audio.",
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
