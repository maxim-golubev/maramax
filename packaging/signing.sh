# Where the release signing keychain lives. Sourced by build_app.sh and
# packaging/create_signing_identity.sh; MARAMAX_SIGNING_DIR moves it.
SIGNING_DIR="${MARAMAX_SIGNING_DIR:-$HOME/.maramax-signing}"
SIGNING_KEYCHAIN="$SIGNING_DIR/signing.keychain-db"
SIGNING_PASSWORD_FILE="$SIGNING_DIR/keychain-password"
