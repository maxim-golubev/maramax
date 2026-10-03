# Where the release signing keychain lives. Sourced by build_app.sh and
# packaging/create_signing_identity.sh; MARAMAX_SIGNING_DIR moves it.
SIGNING_DIR="${MARAMAX_SIGNING_DIR:-$HOME/.maramax-signing}"
# A relative path would name a different folder in each script (build_app.sh
# changes directory first) and another again in `security`, which resolves
# keychain names against ~/Library/Keychains.
if [[ "$SIGNING_DIR" != /* ]]; then
  echo "ERROR: MARAMAX_SIGNING_DIR must be an absolute path, not '$SIGNING_DIR'." >&2
  exit 1
fi
SIGNING_KEYCHAIN="$SIGNING_DIR/signing.keychain-db"
SIGNING_PASSWORD_FILE="$SIGNING_DIR/keychain-password"
# The user's keychain search list as it was before the signing keychain was
# put on it; it exists only until the list is proved restored.
SAVED_LISTING="$SIGNING_DIR/search-list-before-signing"
