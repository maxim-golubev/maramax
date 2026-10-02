"""The update delta between two app bundles: what turns one into the other, exactly.

A delta holds the files and symlinks that are new or changed under files/,
and manifest.json: the paths to delete and the full tree of the new bundle
(every directory, file, and symlink with its permissions). Applying it
deletes, places, and sets permissions, then compares the result with that
tree, so a delta can only produce the bundle it was made from or fail. The
code signature is checked on top of this by the updater.

packaging/create_release.py makes deltas; updater.py applies them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

_FILES = "files"
_MANIFEST = "manifest.json"
_FORMAT = 1

# (kind, content, permissions): content is a file's SHA-256, a link's target, or "" for a directory.
Entry = tuple[str, str, int]


class DeltaError(RuntimeError):
    pass


def delta_name(from_version: str, to_version: str) -> str:
    # A ZIP, but not named .zip: 0.6.1 accepts a release only with exactly one .zip.
    return f"Maramax-{to_version}-from-{from_version}.delta"


def delta_glob(to_version: str) -> str:
    """Every delta to `to_version`, whatever version it starts from."""
    return delta_name("*", to_version)


def entries(app: Path) -> dict[str, Entry]:
    """Every directory, file, and symlink in a bundle, by path relative to it."""
    tree: dict[str, Entry] = {}
    for directory, dirnames, filenames in os.walk(app):
        for name in [*dirnames, *filenames]:
            path = Path(directory) / name
            relative = str(path.relative_to(app))
            if path.is_symlink():
                tree[relative] = ("link", os.readlink(path), 0)
            elif path.is_dir():
                tree[relative] = ("dir", "", stat.S_IMODE(path.stat().st_mode))
            else:
                with path.open("rb") as file:
                    digest = hashlib.file_digest(file, "sha256").hexdigest()
                tree[relative] = ("file", digest, stat.S_IMODE(path.stat().st_mode))
    return tree


def make(old_app: Path, new_app: Path, delta_dir: Path) -> tuple[int, int]:
    """Write into `delta_dir` what turns `old_app` into `new_app`. Returns how
    many paths it adds or changes and how many it deletes."""
    old, new = entries(old_app), entries(new_app)
    carried = sorted(path for path, entry in new.items()
                     if entry[0] != "dir" and old.get(path, ("",))[:2] != entry[:2])
    changed = sum(old.get(path) != entry for path, entry in new.items())
    deleted = sorted(path for path in old if path not in new)
    for relative in carried:
        source, target = new_app / relative, delta_dir / _FILES / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
        else:
            shutil.copy2(source, target)
    (delta_dir / _MANIFEST).write_text(json.dumps({
        "format": _FORMAT, "deleted": deleted, "carried": carried,
        "tree": {path: list(entry) for path, entry in sorted(new.items())},
    }))
    return changed, len(deleted)


def _inside(root: Path, relative: str) -> Path:
    """`root / relative`, refusing anything that could land outside `root`:
    an absolute path, `..`, or a way through a symlink. A bundle's own tree
    never lists a path through a link (the walk does not follow them)."""
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or ".." in parts:
        raise DeltaError(f"The delta names a path outside the app: {relative!r}")
    for depth in range(1, len(parts)):
        if root.joinpath(*parts[:depth]).is_symlink():
            raise DeltaError(f"The delta reaches through a symlink: {relative!r}")
    return root.joinpath(*parts)


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _depth(relative: str) -> int:
    return len(Path(relative).parts)


def apply(delta_dir: Path, app: Path) -> None:
    """Turn `app` (a copy of the version the delta starts from) into the new
    version, then prove it matches the delta's tree. Raises DeltaError."""
    try:
        manifest = json.loads((delta_dir / _MANIFEST).read_text(encoding="utf-8"))
        deleted, carried = list(manifest["deleted"]), set(manifest["carried"])
        tree = {path: (kind, content, int(mode)) for path, (kind, content, mode) in manifest["tree"].items()}
        if manifest.get("format") != _FORMAT:
            raise DeltaError(f"Unknown delta format {manifest.get('format')!r}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DeltaError(f"The delta's manifest is unreadable: {exc}") from exc
    try:
        for relative in sorted(deleted, key=_depth, reverse=True):
            _remove(_inside(app, relative))
        for relative in sorted(tree, key=_depth):
            kind = tree[relative][0]
            target = _inside(app, relative)
            if kind == "dir":
                if target.is_symlink() or target.is_file():
                    target.unlink()
                target.mkdir(exist_ok=True)
            elif relative in carried:
                source = _inside(delta_dir / _FILES, relative)
                if source.is_symlink() != (kind == "link"):
                    raise DeltaError(f"The delta carries {relative!r} as something other than a {kind}")
                _remove(target)
                if kind == "link":
                    target.symlink_to(os.readlink(source))
                else:
                    shutil.copy2(source, target)
        for relative, (kind, _, mode) in tree.items():
            if kind != "link":
                os.chmod(_inside(app, relative), mode)
    except OSError as exc:
        raise DeltaError(f"The delta could not be applied: {exc}") from exc
    result = entries(app)
    if result != tree:
        differing = sorted(set(result) ^ set(tree) | {p for p in result.keys() & tree.keys() if result[p] != tree[p]})
        raise DeltaError(f"The rebuilt app differs from the new version at {len(differing)} paths, "
                         f"for example {differing[:3]}")
