#!/bin/bash
# Create the certificate Maramax releases are signed with: run once, on the
# machine that builds releases. Installed copies only accept an update signed
# with it (updater.SIGNER_CERTIFICATE_SHA1), and macOS keeps the app's
# microphone and Accessibility permissions across builds signed with it.
#
# Everything lives in $MARAMAX_SIGNING_DIR (default ~/.maramax-signing): a
# keychain of its own, its password, and nothing in the repository. Back that
# folder up somewhere safe: without it no installed copy will accept another
# update, and every user would have to install the next version by hand.

set -euo pipefail

SIGNING_DIR="${MARAMAX_SIGNING_DIR:-$HOME/.maramax-signing}"
KEYCHAIN="$SIGNING_DIR/signing.keychain-db"
if [ -e "$KEYCHAIN" ]; then
  echo "A signing keychain already exists at $KEYCHAIN; it is never replaced." >&2
  exit 1
fi

mkdir -p "$SIGNING_DIR"
chmod 700 "$SIGNING_DIR"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cat > "$WORK/certificate.cnf" <<'EOF'
[req]
distinguished_name = subject
x509_extensions = code_signing
prompt = no
[subject]
CN = Maramax Release Signing
[code_signing]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
EOF
# Signatures carry no timestamp, so they are checked against the certificate's
# dates every time: it is made to outlast the app.
openssl req -x509 -newkey rsa:3072 -nodes -days 10950 -config "$WORK/certificate.cnf" \
  -keyout "$WORK/key.pem" -out "$WORK/certificate.pem" 2>/dev/null
P12_PASSWORD="$(openssl rand -hex 16)"
openssl pkcs12 -export -legacy -inkey "$WORK/key.pem" -in "$WORK/certificate.pem" \
  -out "$WORK/identity.p12" -passout "pass:$P12_PASSWORD" 2>/dev/null \
  || openssl pkcs12 -export -inkey "$WORK/key.pem" -in "$WORK/certificate.pem" \
       -out "$WORK/identity.p12" -passout "pass:$P12_PASSWORD"

KEYCHAIN_PASSWORD="$(openssl rand -hex 24)"
( umask 077 && printf '%s' "$KEYCHAIN_PASSWORD" > "$SIGNING_DIR/keychain-password" )
security create-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
security set-keychain-settings "$KEYCHAIN"   # No automatic lock timeout.
security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
security import "$WORK/identity.p12" -k "$KEYCHAIN" -P "$P12_PASSWORD" -T /usr/bin/codesign >/dev/null
# Lets codesign use the key without a dialog.
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN" >/dev/null

SHA1="$(openssl x509 -in "$WORK/certificate.pem" -noout -fingerprint -sha1 | cut -d= -f2 | tr -d :)"
echo "Created $KEYCHAIN"
echo "Certificate SHA-1: $SHA1"
echo "Put it in src/parakeet_dictation/updater.py as SIGNER_CERTIFICATE_SHA1, and back up $SIGNING_DIR."
