"""Package a verified local build without installing or launching dictation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import tomllib


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    bundle = root / "dist" / "Maramax.app"
    info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleShortVersionString"] == version
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(bundle)], check=True)
    # --version exits before creating the GUI, models, or an audio backend.
    output = subprocess.check_output([str(bundle / "Contents" / "MacOS" / "Maramax"), "--version"], text=True)
    assert output.strip() == f"maramax {version}"
    resources = bundle / "Contents" / "Resources"
    bundled_source = resources / "lib" / "python3.12" / "parakeet_dictation"
    hashes = {}
    for source in sorted((root / "src" / "parakeet_dictation").glob("*.py")):
        content = source.read_bytes()
        assert content == (bundled_source / source.name).read_bytes(), f"Stale bundle: {source.name}"
        hashes[str(source.relative_to(root))] = hashlib.sha256(content).hexdigest()
    destination = root / "releases" / f"Maramax-{version}"
    destination.mkdir(parents=True, exist_ok=False)  # Preserve previous releases.
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
    main()
