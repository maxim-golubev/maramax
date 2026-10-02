"""Upload a release made by create_release.py to GitHub, where the app's updater looks for it.

    .venv/bin/python packaging/publish_release.py --notes-file notes.md

The tag (v<version>) is created on HEAD, which must be the clean commit the
release was built from (create_release.py records it) and already pushed:
the updater offers whatever is published as GitHub's latest release. An
existing tag is never reused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tomllib

from create_release import checksum_path, release_paths


class PublishError(RuntimeError):
    pass


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--notes-file", type=Path, required=True, help="Markdown shown in the update prompt")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    destination, archive, checksum = release_paths(root, version)
    for path in (archive, checksum, destination / "build-info.json", args.notes_file):
        if not path.is_file():
            raise PublishError(f"{path} does not exist; run create_release.py first")
    with archive.open("rb") as archive_file:
        digest = hashlib.file_digest(archive_file, "sha256").hexdigest()
    if checksum.read_text().split()[:1] != [digest]:
        raise PublishError(f"{checksum.name} does not match {archive.name}")

    if _git(root, "status", "--porcelain"):
        raise PublishError("The checkout has uncommitted changes; commit and push first")
    commit = _git(root, "rev-parse", "HEAD")
    build = json.loads((destination / "build-info.json").read_text())
    if build.get("dirty") is not False or build.get("commit") != commit:
        raise PublishError(f"{archive.name} was built from {build.get('commit')} "
                           f"({'with' if build.get('dirty') else 'without'} uncommitted changes), not from HEAD "
                           f"{commit}; rebuild and run create_release.py again")
    if subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", "HEAD", "@{upstream}"]).returncode:
        raise PublishError("HEAD is not pushed; push it first")
    tag = f"v{version}"
    if subprocess.run(["git", "-C", str(root), "ls-remote", "--exit-code", "--tags", "origin", f"refs/tags/{tag}"],
                      capture_output=True).returncode == 0:
        raise PublishError(f"The tag {tag} already exists on GitHub; bump the version")

    deltas = sorted(archive.parent.glob(f"Maramax-{version}-from-*.delta"))
    for delta in deltas:
        if not checksum_path(delta).is_file():
            raise PublishError(f"{delta.name} has no checksum; run create_release.py again")
    uploads = [str(path) for asset in (archive, *deltas) for path in (asset, checksum_path(asset))]
    subprocess.run(["gh", "release", "create", tag, *uploads, "--target", commit,
                    "--title", f"Maramax {version}", "--notes-file", str(args.notes_file), "--latest"],
                   cwd=root, check=True)


if __name__ == "__main__":
    try:
        main()
    except (PublishError, subprocess.CalledProcessError) as exc:
        sys.exit(f"Not published: {exc}")
