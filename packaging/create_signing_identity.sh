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

source "$(dirname "$0")/signing.sh"
KEYCHAIN="$SIGNING_KEYCHAIN"
if [ -e "$KEYCHAIN" ]; then
  echo "A signing keychain already exists at $KEYCHAIN; it is never replaced." >&2
  exit 1
fi
if [ -e "$SAVED_LISTING" ]; then
  echo "ERROR: an earlier build or signing setup stopped with the signing keychain on your search list. The list before it was:" >&2
  cat "$SAVED_LISTING" >&2
  echo "Compare with 'security list-keychains -d user', restore it if they differ, then delete $SAVED_LISTING." >&2
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

# `security create-keychain` also puts the new keychain on the user's keychain
# search list, which every app uses to find its passwords. Nothing here needs
# it there (each command names the keychain), and build_app.sh puts it there
# only while it signs. So, as in build_app.sh, the list is checked before it is
# touched, recorded on disk, put back on any exit, and proved restored; a setup
# that fails also deletes the half-made keychain, so this script can run again.
ORIGINAL_LISTING="$(security list-keychains -d user)"
ORIGINAL_KEYCHAINS=()
while IFS= read -r line; do
  line="${line#"${line%%[![:space:]]*}"}"; line="${line%\"}"; line="${line#\"}"
  if [ -z "$line" ] || [ ! -f "$line" ]; then
    echo "ERROR: the keychain search list has an entry that is not a keychain file: '$line'." >&2
    echo "Not touching it. Fix it first (security list-keychains -d user -s <each keychain>)." >&2
    exit 1
  fi
  ORIGINAL_KEYCHAINS+=("$line")
done <<< "$ORIGINAL_LISTING"
restore_keychains() {
  local attempt
  for attempt in 1 2 3; do
    security list-keychains -d user -s "${ORIGINAL_KEYCHAINS[@]}" || true
    if [ "$(security list-keychains -d user)" = "$ORIGINAL_LISTING" ]; then
      rm -f "$SAVED_LISTING"
      security lock-keychain "$KEYCHAIN" || true
      return 0
    fi
    sleep 1
  done
  echo "ERROR: the keychain search list was not restored. It was (also kept in $SAVED_LISTING):" >&2
  echo "$ORIGINAL_LISTING" >&2
  return 1
}
# The keychain is deleted only once the list no longer names it.
discard_keychain() {
  rm -rf "$WORK"
  if restore_keychains; then
    rm -f "$KEYCHAIN" "$SIGNING_PASSWORD_FILE"
    echo "Setup failed and kept nothing; run this script again once the error above is fixed." >&2
  fi
}
printf '%s\n' "$ORIGINAL_LISTING" > "$SAVED_LISTING"
trap discard_keychain EXIT

KEYCHAIN_PASSWORD="$(openssl rand -hex 24)"
( umask 077 && printf '%s' "$KEYCHAIN_PASSWORD" > "$SIGNING_PASSWORD_FILE" )
security create-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
security set-keychain-settings "$KEYCHAIN"   # No automatic lock timeout.
security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
security import "$WORK/identity.p12" -k "$KEYCHAIN" -P "$P12_PASSWORD" -T /usr/bin/codesign >/dev/null
# Lets codesign use the key without a dialog.
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN" >/dev/null

SHA1="$(openssl x509 -in "$WORK/certificate.pem" -noout -fingerprint -sha1 | cut -d= -f2 | tr -d :)"
restore_keychains
trap 'rm -rf "$WORK"' EXIT
echo "Created $KEYCHAIN"
echo "Certificate SHA-1: $SHA1"
echo "Put it in src/parakeet_dictation/updater.py as SIGNER_CERTIFICATE_SHA1, and back up $SIGNING_DIR."
