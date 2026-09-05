#!/bin/bash
# release-mac.sh — the one-command Mac release, once a Developer ID
# certificate exists in the keychain and notary credentials are stored:
#
#   xcrun notarytool store-credentials aloe-notary \
#       --apple-id <id> --team-id <team> --password <app-specific>  (run once)
#   bash scripts/release-mac.sh 1.2.0
#
# Builds, signs with Developer ID (hardened runtime), packages the DMG,
# notarizes, staples, publishes a GitHub release with checksums, and
# updates the site's version feed. Refuses to run without the cert.
set -euo pipefail
cd "$(dirname "$0")/.."

VER="${1:?usage: release-mac.sh <version>}"
DEV_ID="$(security find-identity -v -p codesigning | grep -o '"Developer ID Application: [^"]*"' | head -1 | tr -d '"')"
[ -n "$DEV_ID" ] || { echo "No Developer ID Application certificate in the keychain." >&2; exit 1; }
xcrun notarytool history --keychain-profile aloe-notary --output-format json >/dev/null 2>&1 \
    || { echo "Notary profile 'aloe-notary' missing — run store-credentials first." >&2; exit 1; }
pgrep -f "aloe-audio-capture.*--output" >/dev/null && { echo "Recording in progress — aborting." >&2; exit 1; }

echo "$VER" > VERSION
echo "[1/6] Building the app..."
osascript -e 'tell application "Aloe Scribe" to quit' 2>/dev/null || true
sleep 2
bash scripts/build-app.sh >/dev/null

APP="/Applications/Aloe Scribe.app"
echo "[2/6] Signing inside-out with: $DEV_ID"
ENT="scripts/entitlements.plist"
# Every nested Mach-O first: extension modules, dylibs, then frameworks,
# then executables, then the bundle. --deep is deprecated and skips
# nested bundles, which is exactly what the notary rejected.
# Batched: the vendored dependency set holds hundreds of extension
# modules, and one codesign process per file takes forever.
find "$APP/Contents" -type f \( -name "*.so" -o -name "*.dylib" \) -print0 |
    xargs -0 -n 100 codesign --force --options runtime --timestamp --sign "$DEV_ID"
# pip-shipped Qt frameworks are malformed bundles (no Current symlink),
# so codesign rejects the .framework directory. Sign the framework's
# actual Mach-O binary directly — the notary accepts that.
find "$APP/Contents" -type d -name "*.framework" -print0 |
    while IFS= read -r -d '' fw; do
        base="$(basename "$fw" .framework)"
        for bin in "$fw"/Versions/*/"$base"; do
            [ -f "$bin" ] && codesign --force --options runtime --timestamp \
                --sign "$DEV_ID" "$bin"
        done
    done
codesign --force --options runtime --timestamp --entitlements "$ENT" \
    --sign "$DEV_ID" "$APP/Contents/Resources/bin/aloe-audio-capture"
find "$APP/Contents/MacOS" -type f -print0 |
    while IFS= read -r -d '' x; do
        codesign --force --options runtime --timestamp --entitlements "$ENT" \
            --sign "$DEV_ID" "$x"
    done
codesign --force --options runtime --timestamp --entitlements "$ENT" \
    --sign "$DEV_ID" "$APP"
codesign --verify --strict "$APP"

# Privacy audit — the mac-v1.0.0 recall happened because the DMG bundled the
# builder's personal config.toml. No release leaves this machine until the
# bundle proves it carries only the pristine template.
echo "[audit] Verifying the bundle ships no personal data..."
CFG_DIR="$APP/Contents/Resources/config"
[ "$(ls "$CFG_DIR")" = "config.toml.example" ] \
    || { echo "AUDIT FAIL: $CFG_DIR must hold only config.toml.example" >&2; ls "$CFG_DIR" >&2; exit 1; }
grep -Eq '^local_dir = ""' "$CFG_DIR/config.toml.example" \
    || { echo "AUDIT FAIL: template local_dir is not empty" >&2; exit 1; }
grep -Eq '^ical_url = ""' "$CFG_DIR/config.toml.example" \
    || { echo "AUDIT FAIL: template ical_url is not empty" >&2; exit 1; }
if grep -rliE "onedrive|trace3|cloudstorage|$(whoami)" "$CFG_DIR"; then
    echo "AUDIT FAIL: personal marker found in bundled config" >&2; exit 1
fi

echo "[3/6] Packaging the DMG..."
DMG="dist/AloeScribe-$VER.dmg"
bash scripts/build-dmg.sh "$APP" "$DMG" >/dev/null
codesign --force --sign "$DEV_ID" --timestamp "$DMG"

echo "[4/6] Notarizing (a few minutes)..."
xcrun notarytool submit "$DMG" --keychain-profile aloe-notary --wait
xcrun stapler staple "$DMG"

echo "[5/6] Publishing release mac-v$VER..."
SHA="$(shasum -a 256 "$DMG" | awk '{print $1}')"
gh release create "mac-v$VER" "$DMG" \
    --title "Aloe Scribe for Mac v$VER" \
    --notes "Signed and notarized DMG. SHA-256: $SHA"

echo "[6/6] Updating the site version feed..."
python3 - "$VER" "$SHA" <<'PY'
import json, sys
ver, sha = sys.argv[1], sys.argv[2]
path = "site/version.json"
try:
    data = json.load(open(path))
except Exception:
    data = {}
data["mac"] = {
    "version": ver,
    "url": f"https://github.com/vinnybarbs/Aloe-Scribe/releases/download/mac-v{ver}/AloeScribe-{ver}.dmg",
    "sha256": sha,
}
json.dump(data, open(path, "w"), indent=2)
PY
git add VERSION site/version.json
git commit -m "Release Mac v$VER"
git push
echo "Done. DMG live on the release, site feed updated."
