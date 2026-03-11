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

# ── Remove mlx/scipy/charset_normalizer stubs from the py2app zip ──
# py2app may create .pyc stubs in pythonXY.zip that shadow real packages.
# We strip them and rely on the full packages copied below.
if [ -f "$BUNDLE_ZIP" ]; then
  python - "$BUNDLE_ZIP" <<'PY'
import sys, zipfile, tempfile, shutil, os
src = sys.argv[1]
prefixes = ("mlx/", "scipy/", "charset_normalizer/")
tmp = src + ".tmp"
removed = 0
with zipfile.ZipFile(src, "r") as zin, zipfile.ZipFile(tmp, "w") as zout:
    for item in zin.infolist():
        if any(item.filename.startswith(p) or item.filename == p.rstrip("/") for p in prefixes):
            removed += 1
            continue
        zout.writestr(item, zin.read(item.filename))
if removed:
    shutil.move(tmp, src)
    print(f"Stripped {removed} stub entries from {os.path.basename(src)}")
else:
    os.unlink(tmp)
    print(f"No stubs to strip from {os.path.basename(src)}")
PY
fi

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
  "$BUNDLE_SITE_PACKAGES/parakeet_dictation/__init__.py" \
  "$MLX_PACKAGE_DEST/_reprlib_fix.py" \
  "$MLX_PACKAGE_DEST/core.cpython-312-darwin.so"; do
  if [ ! -f "$check_path" ]; then
    echo "ERROR: Missing expected file: $check_path" >&2
    exit 1
  fi
done

codesign --force --sign - "$ROOT_DIR/dist/Maramax.app"
