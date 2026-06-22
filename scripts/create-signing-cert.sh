#!/bin/bash
# create-signing-cert.sh — Create a stable self-signed code-signing identity
# ("Aloe Scribe Local Signing") in the login keychain.
#
# Why: the app + helper are otherwise ad-hoc signed, so every rebuild changes
# the signature and macOS revokes the app's Screen Recording / Microphone (TCC)
# permissions — forcing a re-grant after each update. A stable signing identity
# gives a stable code requirement, so macOS keeps the grants across rebuilds.
#
# build-app.sh / build-helper.sh auto-detect and use this identity if present,
# else fall back to ad-hoc. Run once; safe to re-run (no-op if it exists).

set -e
NAME="Aloe Scribe Local Signing"

if security find-identity -p codesigning 2>/dev/null | grep -q "$NAME"; then
    echo "Signing identity already exists: $NAME — nothing to do."
    exit 0
fi

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
cat > "$WORK/cert.conf" <<EOF
[ req ]
distinguished_name = dn
x509_extensions = ext
prompt = no
[ dn ]
CN = $NAME
[ ext ]
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
basicConstraints = critical, CA:false
EOF

echo "Generating self-signed code-signing certificate..."
openssl req -x509 -newkey rsa:2048 -keyout "$WORK/key.pem" -out "$WORK/cert.pem" \
    -days 3650 -nodes -config "$WORK/cert.conf" 2>/dev/null
# OpenSSL 3.x otherwise writes a PKCS#12 macOS' importer can't verify:
#   -legacy      → encryption algorithms macOS understands
#   -macalg sha1 → MAC algorithm macOS understands
# A real (throwaway) password is also needed — empty-password p12s fail import.
PW="aloe-scribe-local"
LEGACY=""; MACALG=""
openssl pkcs12 -help 2>&1 | grep -q -- "-legacy"  && LEGACY="-legacy"
openssl pkcs12 -help 2>&1 | grep -q -- "-macalg"  && MACALG="-macalg sha1"
openssl pkcs12 -export $LEGACY $MACALG -out "$WORK/id.p12" \
    -inkey "$WORK/key.pem" -in "$WORK/cert.pem" -passout pass:"$PW" -name "$NAME" 2>/dev/null

KC="$HOME/Library/Keychains/login.keychain-db"
# -T pre-authorizes codesign/security to use the private key without prompting.
security import "$WORK/id.p12" -k "$KC" -P "$PW" -T /usr/bin/codesign -T /usr/bin/security >/dev/null

echo "Verifying the identity is registered for code signing..."
if security find-identity -p codesigning 2>/dev/null | grep -q "$NAME"; then
    # Self-signed → shows CSSMERR_TP_NOT_TRUSTED; that's fine, codesign still
    # signs with it (trust only matters for Gatekeeper verification).
    echo "✓ Signing identity ready: $NAME"
    echo "  Rebuild with: bash scripts/build-app.sh  (uses it automatically)"
else
    echo "✗ Identity not found after import." >&2
    echo "  Fallback: create it via Keychain Access → Certificate Assistant →" >&2
    echo "  Create a Certificate (Name: $NAME, Identity Type: Self Signed Root," >&2
    echo "  Certificate Type: Code Signing)." >&2
    exit 1
fi
