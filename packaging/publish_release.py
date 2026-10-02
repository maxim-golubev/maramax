"""Upload a release made by create_release.py to GitHub, where the app's updater looks for it.

    .venv/bin/python packaging/publish_release.py --notes-file notes.md

The tag (v<version>) is created on the commit the release was built from,
which must already be pushed: the updater offers whatever is published as
GitHub's latest release, so nothing is published from an unpushed or dirty
checkout, and an existing tag is never replaced.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
import tomllib


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
    archive = root / "releases" / f"Maramax-{version}.zip"
    checksum = archive.with_name(archive.name + ".sha256")
    for path in (archive, checksum, args.notes_file):
        if not path.is_file():
            raise PublishError(f"{path} does not exist; run create_release.py first")
    with archive.open("rb") as archive_file:
        digest = hashlib.file_digest(archive_file, "sha256").hexdigest()
    if checksum.read_text().split()[0] != digest:
        raise PublishError(f"{checksum.name} does not match {archive.name}")

    if _git(root, "status", "--porcelain"):
        raise PublishError("The checkout has uncommitted changes; commit and push first")
    commit = _git(root, "rev-parse", "HEAD")
    if subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", "HEAD", "@{upstream}"]).returncode:
        raise PublishError("HEAD is not pushed; push it first")
    tag = f"v{version}"
    if subprocess.run(["gh", "release", "view", tag], cwd=root, capture_output=True).returncode == 0:
        raise PublishError(f"Release {tag} already exists on GitHub")

    subprocess.run(["gh", "release", "create", tag, str(archive), str(checksum), "--target", commit,
                    "--title", f"Maramax {version}", "--notes-file", str(args.notes_file), "--latest"],
                   cwd=root, check=True)


if __name__ == "__main__":
    try:
        main()
    except (PublishError, subprocess.CalledProcessError) as exc:
        sys.exit(f"Not published: {exc}")
