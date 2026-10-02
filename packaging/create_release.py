"""Package a verified local build without installing or launching dictation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tomllib


class ReleaseError(RuntimeError):
    pass


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    bundle = root / "dist" / "Maramax.app"
    destination = root / "releases" / f"Maramax-{version}"
    if destination.exists():
        # Previous releases are never overwritten; a half-made one from a
        # failed run has to be removed by hand, deliberately.
        raise ReleaseError(f"{destination} already exists; remove it (and its .zip) to release {version} again")

    info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    if info["CFBundleShortVersionString"] != version:
        raise ReleaseError(f"Bundle is version {info['CFBundleShortVersionString']}, pyproject.toml says {version}")
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(bundle)], check=True)
    # --version exits before creating the GUI, models, or an audio backend.
    output = subprocess.check_output([str(bundle / "Contents" / "MacOS" / "Maramax"), "--version"], text=True)
    if output.strip() != f"maramax {version}":
        raise ReleaseError(f"Bundle reports {output.strip()!r}, expected 'maramax {version}'")
    resources = bundle / "Contents" / "Resources"
    bundled_source = resources / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "parakeet_dictation"
    hashes = {}
    for source in sorted((root / "src" / "parakeet_dictation").glob("*.py")):
        content = source.read_bytes()
        if content != (bundled_source / source.name).read_bytes():
            raise ReleaseError(f"Stale bundle: {source.name} differs from the checkout; run build_app.sh")
        hashes[str(source.relative_to(root))] = hashlib.sha256(content).hexdigest()
    # A build whose own check failed still leaves a signed bundle behind, so
    # the check is repeated here rather than assumed.
    subprocess.run([sys.executable, str(root / "packaging" / "check_bundle.py"), "--bundle", str(bundle)],
                   check=True, stdout=subprocess.DEVNULL)

    destination.mkdir(parents=True)
    subprocess.run(["ditto", str(bundle), str(destination / "Maramax.app")], check=True)
    shutil.copyfile(root / "docs" / "LAUNCH.md", destination / "START HERE.md")
    (destination / "build-info.json").write_text(json.dumps({
        "version": version, "architecture": "arm64", "signing": "local ad-hoc",
        "source_sha256": hashes,
    }, indent=2) + "\n")
    archive = destination.parent / f"{destination.name}.zip"
    subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(destination), str(archive)], check=True)
    with archive.open("rb") as archive_file:
        digest = hashlib.file_digest(archive_file, "sha256").hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n")
    print(destination)
    print(archive)


if __name__ == "__main__":
    try:
        main()
    except ReleaseError as exc:
        sys.exit(f"Not released: {exc}")
