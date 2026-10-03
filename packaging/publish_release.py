"""Upload a release made by create_release.py to GitHub, where the app's updater looks for it.

    .venv/bin/python packaging/publish_release.py --notes-file ~/maramax-notes.md

The notes file is kept outside the checkout, which must be clean. The tag
(v<version>) is created on HEAD, which must be the commit the release was
built from (create_release.py records it), holding exactly the sources the
release bundles, and already pushed: the updater offers whatever is published
as GitHub's latest release. An existing tag is never reused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tomllib

from create_release import (ROOT, APP_SOURCES, assets_path, checksum_path, published_path, release_paths,
                            steps_to_release_again)

sys.path.insert(0, str(ROOT / "src"))
from parakeet_dictation.updater import UpdateError, parse_checksum  # noqa: E402


class PublishError(RuntimeError):
    pass


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # Resolved here: it is checked from this directory but read by gh from the repository's.
    parser.add_argument("--notes-file", type=lambda text: Path(text).resolve(), required=True,
                        help="Markdown shown in the update prompt")
    return parser.parse_args(argv)


def committed_sources(root: Path, commit: str) -> dict[str, str]:
    """The SHA-256 of each of the app's sources as `commit` holds them, keyed
    as create_release.py keys them in build-info.json."""
    sources = {}
    for entry in filter(None, _git(root, "ls-tree", "-z", commit, "--", f"{APP_SOURCES.as_posix()}/").split("\0")):
        details, path = entry.split("\t", 1)
        _, kind, blob = details.split()
        if kind == "blob" and path.endswith(".py"):
            content = subprocess.check_output(["git", "-C", str(root), "cat-file", "blob", blob])
            sources[path] = hashlib.sha256(content).hexdigest()
    return sources


def differing_sources(bundled: dict[str, str], committed: dict[str, str]) -> list[str]:
    """The sources whose bundled copy is not the committed one, including any
    file only one side has. An edit made while the app was being built, then
    undone, leaves a clean checkout and a bundle that no commit holds."""
    return sorted(path for path in bundled.keys() | committed.keys() if bundled.get(path) != committed.get(path))


def origin_has_tag(root: Path, tag: str) -> bool:
    """Whether GitHub already has `tag`. A question git could not put to origin
    (network, credentials) stops the publish instead of reading as "no"."""
    asked = subprocess.run(["git", "-C", str(root), "ls-remote", "--exit-code", "--tags", "origin", f"refs/tags/{tag}"],
                           capture_output=True, text=True)
    if asked.returncode not in (0, 2):  # With --exit-code, 2 is "no such ref" and anything else a failure.
        raise PublishError(f"Could not ask origin whether {tag} exists (git ls-remote exit {asked.returncode}): "
                           f"{asked.stderr.strip()}")
    return asked.returncode == 0


def main() -> None:
    args = arguments()
    root = ROOT
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    destination, archive, _ = release_paths(root, version)
    listing = assets_path(root, version)
    if not args.notes_file.is_file():
        raise PublishError(f"The release notes {args.notes_file} do not exist; write them, outside the checkout, first")
    for path in (listing, destination / "build-info.json"):
        if not path.is_file():
            raise PublishError(f"{path} does not exist; run create_release.py first")
    assets = json.loads(listing.read_text())
    if archive.name not in assets:
        raise PublishError(f"{listing.name} does not list {archive.name}")
    uploads = []
    for name, expected in assets.items():
        asset = archive.parent / name
        try:
            with asset.open("rb") as file:
                digest = hashlib.file_digest(file, "sha256").hexdigest()
            published = parse_checksum(checksum_path(asset).read_text())
        except (OSError, UpdateError) as exc:
            raise PublishError(f"{name} or its checksum cannot be read: {exc}") from exc
        if not digest == expected == published:
            raise PublishError(f"{name} is not the file create_release.py made; "
                               f"{steps_to_release_again(root, version)}")
        uploads += [str(asset), str(checksum_path(asset))]

    changes = _git(root, "status", "--porcelain")
    if changes:
        raise PublishError(f"The checkout has uncommitted changes; commit and push first:\n{changes}")
    commit = _git(root, "rev-parse", "HEAD")
    build = json.loads((destination / "build-info.json").read_text())
    if build.get("dirty") is not False or build.get("commit") != commit:
        raise PublishError(f"{archive.name} was built from {build.get('commit')} "
                           f"({'with' if build.get('dirty') else 'without'} uncommitted changes), not from HEAD "
                           f"{commit}; run build_app.sh, then {steps_to_release_again(root, version)}")
    bundled = build.get("source_sha256")
    if not isinstance(bundled, dict):
        raise PublishError(f"{destination / 'build-info.json'} records no source hashes; "
                           f"{steps_to_release_again(root, version)}")
    differing = differing_sources(bundled, committed_sources(root, commit))
    if differing:
        raise PublishError(f"{archive.name} bundles sources that {commit} does not hold: {', '.join(differing)}; "
                           f"run build_app.sh, then {steps_to_release_again(root, version)}")
    if subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", "HEAD", "@{upstream}"]).returncode:
        raise PublishError("HEAD is not pushed; push it first")
    tag = f"v{version}"
    if origin_has_tag(root, tag):
        raise PublishError(f"The tag {tag} already exists on GitHub; bump the version")

    subprocess.run(["gh", "release", "create", tag, *uploads, "--target", commit,
                    "--title", f"Maramax {version}", "--notes-file", str(args.notes_file), "--latest"],
                   cwd=root, check=True)
    # Copies can now be at this version: the next release's delta starts here.
    published_path(root, version).write_text(f"{tag} {commit}\n")


if __name__ == "__main__":
    try:
        main()
    except (PublishError, subprocess.CalledProcessError) as exc:
        sys.exit(f"Not published: {exc}")
