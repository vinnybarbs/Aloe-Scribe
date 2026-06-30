# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Aloe Scribe on Windows.
# Build:  scripts\build-windows.ps1   (wraps  pyinstaller aloe-scribe-windows.spec)
#
# This is the Windows analogue of setup.py (which is py2app, macOS only). It is
# never used on macOS.
#
# The tricky dependencies are ctranslate2 (the CUDA/CPU inference libs),
# faster_whisper (ships a Silero VAD model + assets), av (PyAV's bundled FFmpeg
# DLLs), and PyAudioWPatch. collect_all pulls their binaries and data files so
# the frozen .exe has everything. If the built app fails with a missing module
# or data file, add it to hiddenimports / datas and rebuild on the machine.

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [
    ("assets/icon.png", "assets"),
    ("config/config.toml", "config"),
]
binaries = []
hiddenimports = [
    "transcriber_faster_whisper",  # selected via a dynamic import in main.py
    "recorder_windows",
    "ui_windows",
]

for pkg in ("ctranslate2", "faster_whisper", "av", "pyaudiowpatch", "tokenizers"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        # Package not installed at build time. The build will surface it.
        pass

hiddenimports += collect_submodules("PyQt6")

block_cipher = None

a = Analysis(
    ["src/main.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Apple/Linux-only modules that must never be pulled into the Windows build.
    excludes=[
        "recorder_mac",
        "native_tray",
        "ui",            # GTK/AppIndicator3, Linux only
        "recorder",      # PulseAudio, Linux only
        "parakeet_mlx",
        "mlx",
        "gi",
        "AppKit",
        "Foundation",
        "objc",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Aloe Scribe",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,           # GUI app, no console window
    icon="assets/icon.ico",  # see build-windows.ps1 (generated from icon.png)
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Aloe Scribe",
)
