#!/bin/bash

set -euo pipefail

cd "$(dirname "$0")"
ROOT_DIR="$(pwd)"

if [ -d ".venv" ]; then
  source .venv/bin/activate
fi

python - <<'PY'
from pathlib import Path
import shutil

root = Path.cwd()
for name in ("build", "dist"):
    path = root / name
    if path.exists():
        shutil.rmtree(path)
PY

PYTHON_SHORT_VERSION="$(python - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
PY
)"

cd packaging

python setup.py py2app \
  --dist-dir "$ROOT_DIR/dist" \
  --bdist-base "$ROOT_DIR/build" \
  "$@"

BUNDLE_RESOURCES="$ROOT_DIR/dist/Maramax.app/Contents/Resources"
BUNDLE_SITE_PACKAGES="$BUNDLE_RESOURCES/lib/python$PYTHON_SHORT_VERSION"
BUNDLE_DYNLOAD="$BUNDLE_SITE_PACKAGES/lib-dynload"
VENV_SITE_PACKAGES="$ROOT_DIR/.venv/lib/python$PYTHON_SHORT_VERSION/site-packages"
BUNDLE_ZIP="$BUNDLE_RESOURCES/lib/python${PYTHON_SHORT_VERSION//./}.zip"

# ── Rewrite the py2app zip: no stubs, and the same bytes for the same code ──
# py2app may create .pyc stubs in pythonXY.zip that shadow real packages; they
# are stripped and the full packages copied below are used instead. py2app
# also stamps the build time into every .pyc header and zip entry, which made
# this 50 MB file differ in every build and every update delta. The zip holds
# no .py sources for those timestamps to be checked against, so they are
# zeroed and the entry dates fixed.
if [ -f "$BUNDLE_ZIP" ]; then
  python - "$BUNDLE_ZIP" <<'PY'
import sys, zipfile, shutil, os
src = sys.argv[1]
prefixes = ("mlx/", "scipy/", "charset_normalizer/")
tmp = src + ".tmp"
removed = 0
with zipfile.ZipFile(src, "r") as zin, zipfile.ZipFile(tmp, "w") as zout:
    names = set(zin.namelist())
    for item in zin.infolist():
        if any(item.filename.startswith(p) or item.filename == p.rstrip("/") for p in prefixes):
            removed += 1
            continue
        data = zin.read(item.filename)
        if (item.filename.endswith(".pyc") and len(data) >= 16 and data[4:8] == b"\0\0\0\0"
                and item.filename[:-1] not in names):
            data = data[:8] + b"\0\0\0\0" + data[12:]  # A timestamp .pyc: drop the build time.
        entry = zipfile.ZipInfo(item.filename, date_time=(1980, 1, 1, 0, 0, 0))
        entry.compress_type, entry.external_attr = item.compress_type, item.external_attr
        zout.writestr(entry, data)
shutil.move(tmp, src)
print(f"Rewrote {os.path.basename(src)} reproducibly; stripped {removed} stub entries")
PY
fi
# The app's own bytecode is compiled here from the bundled sources. py2app
# copies the checkout's git-ignored __pycache__, which holds whatever the last
# test run or another Python version left, stamped with file times that change
# with every checkout or stash. Hash-based .pyc depend only on the source bytes
# and are checked against them at import; the fixed hash seed and recorded
# path keep two builds of one commit identical wherever they are built.
find "$BUNDLE_SITE_PACKAGES/parakeet_dictation" -name __pycache__ -type d -prune -exec rm -rf {} +
PYTHONHASHSEED=0 python -m compileall -q -d parakeet_dictation --invalidation-mode checked-hash \
  "$BUNDLE_SITE_PACKAGES/parakeet_dictation"
# py2app's bootstrap site.pyc carries the same build time, and sits sourceless.
python - "$BUNDLE_RESOURCES/site.pyc" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
data = path.read_bytes()
if data[4:8] == b"\0\0\0\0" and not path.with_suffix(".py").exists():
    path.write_bytes(data[:8] + b"\0\0\0\0" + data[12:])
PY

# ── Copy full mlx package from venv ──
# Place in site-packages so it's found on sys.path.
MLX_PACKAGE_DEST="$BUNDLE_SITE_PACKAGES/mlx"
if [ -d "$MLX_PACKAGE_DEST" ]; then
  rm -rf "$MLX_PACKAGE_DEST"
fi
ditto "$VENV_SITE_PACKAGES/mlx" "$MLX_PACKAGE_DEST"

# Also place in lib-dynload for the C extension lookup.
if [ -d "$BUNDLE_DYNLOAD/mlx" ]; then
  rm -rf "$BUNDLE_DYNLOAD/mlx"
fi
mkdir -p "$BUNDLE_DYNLOAD/mlx"
ditto "$VENV_SITE_PACKAGES/mlx" "$BUNDLE_DYNLOAD/mlx"

# Copy scipy
SCIPY_PACKAGE_SOURCE="$VENV_SITE_PACKAGES/scipy"
SCIPY_PACKAGE_DEST="$BUNDLE_SITE_PACKAGES/scipy"
if [ -d "$SCIPY_PACKAGE_SOURCE" ]; then
  mkdir -p "$(dirname "$SCIPY_PACKAGE_DEST")"
  ditto "$SCIPY_PACKAGE_SOURCE" "$SCIPY_PACKAGE_DEST"
fi

# Copy charset_normalizer
CHARSET_PACKAGE_SOURCE="$VENV_SITE_PACKAGES/charset_normalizer"
CHARSET_PACKAGE_DEST="$BUNDLE_SITE_PACKAGES/charset_normalizer"
if [ -d "$CHARSET_PACKAGE_SOURCE" ]; then
  mkdir -p "$(dirname "$CHARSET_PACKAGE_DEST")"
  ditto "$CHARSET_PACKAGE_SOURCE" "$CHARSET_PACKAGE_DEST"
fi

# ── Verify the bundle contains critical files ──
for check_path in \
  "$BUNDLE_RESOURCES/assets/menu_icon.png" \
  "$BUNDLE_RESOURCES/Maramax.icns" \
  "$BUNDLE_RESOURCES/Assets.car" \
  "$BUNDLE_SITE_PACKAGES/parakeet_dictation/__init__.py" \
  "$MLX_PACKAGE_DEST/_reprlib_fix.py" \
  "$MLX_PACKAGE_DEST/core.cpython-312-darwin.so"; do
  if [ ! -f "$check_path" ]; then
    echo "ERROR: Missing expected file: $check_path" >&2
    exit 1
  fi
done

# ── Sign ──
# With the release certificate (packaging/create_signing_identity.sh) when it
# is on this machine: installed copies accept only such a build as an update.
# Otherwise ad hoc, which runs here but cannot be published.
source "$ROOT_DIR/packaging/signing.sh"
if [ -f "$SIGNING_KEYCHAIN" ]; then
  SIGNER_SHA1="$(PYTHONPATH="$ROOT_DIR/src" python -c 'from parakeet_dictation.updater import SIGNER_CERTIFICATE_SHA1; print(SIGNER_CERTIFICATE_SHA1)')"
  # codesign finds identities only in the user's keychain search list (not
  # with --keychain, not with another preferences home), so the signing
  # keychain is appended to it for this one call. That list is what every
  # app uses to find its passwords: it is checked before it is touched,
  # recorded on disk, put back on any exit, and proved restored afterwards.
  if [ -e "$SAVED_LISTING" ]; then
    echo "ERROR: an earlier build or signing setup stopped with the signing keychain on your search list. The list before it was:" >&2
    cat "$SAVED_LISTING" >&2
    echo "Compare with 'security list-keychains -d user', restore it if they differ, then delete $SAVED_LISTING." >&2
    exit 1
  fi
  ORIGINAL_LISTING="$(security list-keychains -d user)"
  ORIGINAL_KEYCHAINS=()
  while IFS= read -r line; do
    line="${line#"${line%%[![:space:]]*}"}"; line="${line%\"}"; line="${line#\"}"
    if [ -z "$line" ] || [ ! -f "$line" ]; then
      echo "ERROR: the keychain search list has an entry that is not a keychain file: '$line'." >&2
      echo "Not touching it. Fix it first (security list-keychains -d user -s <each keychain>)." >&2
      exit 1
    fi
    # Restoring a list that already holds it would keep it there for good.
    if [ "$line" -ef "$SIGNING_KEYCHAIN" ]; then
      echo "ERROR: the signing keychain is already on your keychain search list, where it should never stay." >&2
      echo "Not touching it. Remove it first (security list-keychains -d user -s <each other keychain, in order>)." >&2
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
        security lock-keychain "$SIGNING_KEYCHAIN" || true
        return 0
      fi
      sleep 1
    done
    echo "ERROR: the keychain search list was not restored. It was (also kept in $SAVED_LISTING):" >&2
    echo "$ORIGINAL_LISTING" >&2
    return 1
  }
  printf '%s\n' "$ORIGINAL_LISTING" > "$SAVED_LISTING"
  trap restore_keychains EXIT
  security unlock-keychain -p "$(cat "$SIGNING_PASSWORD_FILE")" "$SIGNING_KEYCHAIN"
  security list-keychains -d user -s "${ORIGINAL_KEYCHAINS[@]}" "$SIGNING_KEYCHAIN"
  codesign --force --sign "$SIGNER_SHA1" "$ROOT_DIR/dist/Maramax.app"
  restore_keychains
  trap - EXIT
else
  echo "WARNING: no signing keychain at $SIGNING_KEYCHAIN; signing ad hoc. This build cannot be published." >&2
  codesign --force --sign - "$ROOT_DIR/dist/Maramax.app"
fi

# Exercise the installed dependencies and native view without starting the app,
# opening a microphone, registering shortcuts, or loading model weights.
# A bundle that fails is moved aside, so nothing can install or release it.
if ! python "$ROOT_DIR/packaging/check_bundle.py" --bundle "$ROOT_DIR/dist/Maramax.app"; then
  rm -rf "$ROOT_DIR/dist/Maramax.app.failed-check"
  mv "$ROOT_DIR/dist/Maramax.app" "$ROOT_DIR/dist/Maramax.app.failed-check"
  echo "ERROR: bundle check failed; the bundle is at dist/Maramax.app.failed-check" >&2
  exit 1
fi
