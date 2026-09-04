#!/bin/bash
# build-app.sh — Build the macOS .app bundle for Aloe Scribe via py2app.
#
# We use py2app (not a launcher .app) because Tahoe's NSStatusItem filter +
# TCC attribution only treat the .app properly when the bundle has a "real
# app" structure — bundled Python framework, lib/python3.12 trees, py2app's
# native launcher, the lot. Launcher .apps (bash or Swift exec'ing a venv
# Python) end up with a separate `aloe-python` TCC entity AND a hidden
# tray icon. py2app builds avoid both.
#
# mlx + parakeet_mlx + huggingface_hub are explicitly *excluded* from the
# frozen bundle (mlx's namespace-package layout breaks py2app's modulegraph).
# main.py prepends the dev venv's site-packages to sys.path at runtime so
# those imports resolve from there.
#
# Side effects:
#   - Compiles bin/aloe-audio-capture via scripts/build-helper.sh
#   - Runs py2app, producing dist/Aloe Scribe.app
#   - Code-signs the bundled helper + .app with stable ad-hoc identifiers
#     (com.aloescribe.audio-capture + com.aloescribe.app)
#   - Installs to /Applications/Aloe Scribe.app
#
# Usage: bash scripts/build-app.sh

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

APP_NAME="Aloe Scribe"
APP_IDENTIFIER="com.aloescribe.app"
HELPER_IDENTIFIER="com.aloescribe.audio-capture"
APP_DIR="$PROJECT_DIR/dist/${APP_NAME}.app"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python3"

if [ ! -x "$VENV_PYTHON" ]; then
    echo "Error: $VENV_PYTHON not found. Run scripts/install-mac.sh first." >&2
    exit 1
fi

# config/config.toml is per-user and untracked. py2app bundles it (see setup.py
# DATA_FILES), so it must exist before the build. On a fresh clone, seed it
# from the template.
if [ ! -f "$PROJECT_DIR/config/config.toml" ]; then
    cp "$PROJECT_DIR/config/config.toml.example" "$PROJECT_DIR/config/config.toml"
    echo "Created config/config.toml from template."
fi

# 1. Compile + sign the Swift audio-capture helper (committed copy in bin/
#    gets refreshed). py2app picks it up via DATA_FILES in setup.py and
#    drops it at Contents/Resources/bin/aloe-audio-capture inside the .app.
bash scripts/build-helper.sh

# 2. Quit any running instance and clean previous build artifacts.
echo "Quitting any running instance..."
osascript -e "tell application \"${APP_NAME}\" to quit" >/dev/null 2>&1 || true
sleep 1
pkill -f "$APP_DIR/Contents/MacOS" 2>/dev/null || true
rm -rf "$PROJECT_DIR/build" "$PROJECT_DIR/dist"

# 3. Run py2app.
echo "Running py2app (this takes ~30–60s)..."
"$VENV_PYTHON" setup.py py2app 2>&1 | tail -5

if [ ! -d "$APP_DIR" ]; then
    echo "py2app build failed — $APP_DIR not produced." >&2
    exit 1
fi

# 4. Code-sign the helper (inside the bundle) with its stable identifier,
#    then the bundle with com.aloescribe.app. We do NOT use --deep so the
#    helper keeps its own identifier — but both live under the same .app
#    so TCC treats them as one Aloe Scribe identity at the bundle level.
#
#    Sign with a stable self-signed identity if one exists, so macOS keeps
#    granted TCC permissions (Screen Recording / Mic) across rebuilds. Falls
#    back to ad-hoc ("-") otherwise — which works but resets permissions on
#    every rebuild. Create the identity once: scripts/create-signing-cert.sh
SIGN_ID="${ALOE_SIGN_ID:-}"
if [ -z "$SIGN_ID" ]; then
    if security find-identity -p codesigning 2>/dev/null | grep -q "Aloe Scribe Local Signing"; then
        SIGN_ID="Aloe Scribe Local Signing"
    else
        SIGN_ID="-"
    fi
fi
echo "Code-signing identity: $SIGN_ID"

HELPER_IN_BUNDLE="$APP_DIR/Contents/Resources/bin/aloe-audio-capture"
if [ -f "$HELPER_IN_BUNDLE" ]; then
    echo "Signing bundled helper (${HELPER_IDENTIFIER})..."
    chmod +x "$HELPER_IN_BUNDLE"
    codesign --force --sign "$SIGN_ID" --identifier "${HELPER_IDENTIFIER}" "$HELPER_IN_BUNDLE"
else
    echo "Warning: bundled helper not found at $HELPER_IN_BUNDLE" >&2
fi

echo "Signing .app bundle (${APP_IDENTIFIER})..."
codesign --force --sign "$SIGN_ID" --identifier "${APP_IDENTIFIER}" "$APP_DIR"

# 4b. Prune PySide6 to what the app uses (QtCore/QtGui/QtWidgets and
# their support). py2app copies the whole 1.1 GB toolbox, including Qt's
# own developer apps, whose unsigned binaries also fail notarization.
PYSIDE="$APP_DIR/Contents/Resources/lib/python3.12/PySide6"
if [ -d "$PYSIDE" ]; then
    python3 - "$PYSIDE" <<'PRUNE'
import pathlib, shutil, sys

ps = pathlib.Path(sys.argv[1])
KEEP_MODULES = {"QtCore", "QtGui", "QtWidgets", "QtPrintSupport", "QtDBus"}
KEEP_TOP = {
    "Qt", "__init__.py", "_config.py", "_git_pyside_version.py",
    "support", "scripts", "PySide6_Essentials.json", "PySide6_Addons.json",
} | {f"{m}.abi3.so" for m in KEEP_MODULES} | {f"{m}.pyi" for m in KEEP_MODULES}
for item in ps.iterdir():
    if item.name in KEEP_TOP:
        continue
    shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink(missing_ok=True)

qt = ps / "Qt"
shutil.rmtree(qt / "qml", ignore_errors=True)
shutil.rmtree(qt / "libexec", ignore_errors=True)
lib = qt / "lib"
if lib.exists():
    keep_fw = {f"Qt{m[2:]}" if False else m for m in KEEP_MODULES}
    keep_fw = {"QtCore", "QtGui", "QtWidgets", "QtPrintSupport", "QtDBus"}
    for fw in lib.iterdir():
        name = fw.name.replace(".framework", "")
        if name not in keep_fw:
            shutil.rmtree(fw, ignore_errors=True)
plugins = qt / "plugins"
KEEP_PLUGINS = {"platforms", "styles", "imageformats", "iconengines"}
if plugins.exists():
    for pl in plugins.iterdir():
        if pl.name not in KEEP_PLUGINS:
            shutil.rmtree(pl, ignore_errors=True)
print("PySide6 pruned")
PRUNE
    du -sh "$PYSIDE" | awk '{print "  PySide6 now " $1}'
fi

# 5. Install to /Applications.
echo "Installing to /Applications/${APP_NAME}.app..."
rm -rf "/Applications/${APP_NAME}.app"
cp -R "$APP_DIR" "/Applications/${APP_NAME}.app"

echo ""
echo "Done. Open from Spotlight or /Applications."
codesign -dv "/Applications/${APP_NAME}.app" 2>&1 | grep -E "Identifier|Signature"
codesign -dv "/Applications/${APP_NAME}.app/Contents/Resources/bin/aloe-audio-capture" 2>&1 | grep -E "Identifier|Signature" || true
