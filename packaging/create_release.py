"""Package a verified local build without installing or launching dictation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import tomllib


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# The updater's own rules: what it accepts, and the delta format it reads.
from parakeet_dictation.updater import delta_name, make_delta, signer_requirement, version_key  # noqa: E402


class ReleaseError(RuntimeError):
    pass


def release_paths(root: Path, version: str) -> tuple[Path, Path, Path]:
    """Where a release's folder, ZIP, and ZIP checksum are written."""
    destination = root / "releases" / f"Maramax-{version}"
    archive = destination.parent / f"{destination.name}.zip"
    return destination, archive, archive.with_suffix(".zip.sha256")


def checksum_path(asset: Path) -> Path:
    return asset.with_name(asset.name + ".sha256")


def previous_release(root: Path, version: str) -> tuple[str, Path] | None:
    """The newest earlier release on this machine signed with the release
    certificate: copies of it can update with a delta."""
    found = []
    for info in (root / "releases").glob("Maramax-*/build-info.json"):
        build = json.loads(info.read_text())
        if (build.get("signing") == "Maramax release certificate" and (info.parent / "Maramax.app").is_dir()
                and version_key(build["version"]) < version_key(version)):
            found.append((version_key(build["version"]), build["version"], info.parent / "Maramax.app"))
    return max(found)[1:] if found else None


def write_checksum(asset: Path) -> None:
    with asset.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    checksum_path(asset).write_text(f"{digest}  {asset.name}\n")


def main() -> None:
    root = ROOT
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    bundle = root / "dist" / "Maramax.app"
    destination, archive, checksum = release_paths(root, version)
    if destination.exists():
        # Previous releases are never overwritten; a half-made one from a
        # failed run has to be removed by hand, deliberately.
        raise ReleaseError(f"{destination} already exists; remove it (and its .zip) to release {version} again")

    info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    if info["CFBundleShortVersionString"] != version:
        raise ReleaseError(f"Bundle is version {info['CFBundleShortVersionString']}, pyproject.toml says {version}")
    # Installed copies accept only an update signed with the release certificate.
    signed = subprocess.run(["codesign", "--verify", "--deep", "--strict",
                             f"-R={signer_requirement(info['CFBundleIdentifier'])}", str(bundle)])
    if signed.returncode != 0:
        raise ReleaseError("The bundle is not signed with the release certificate; build it where "
                           "~/.maramax-signing exists (packaging/create_signing_identity.sh)")
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
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    dirty = bool(subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"], text=True).strip())
    # publish_release.py publishes only a build of a clean, pushed commit.
    (destination / "build-info.json").write_text(json.dumps({
        "version": version, "architecture": "arm64", "signing": "Maramax release certificate",
        "commit": commit, "dirty": dirty, "source_sha256": hashes,
    }, indent=2) + "\n")
    subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(destination), str(archive)], check=True)
    write_checksum(archive)
    print(destination)
    print(archive)

    previous = previous_release(root, version)
    if previous is not None:
        old_version, old_app = previous
        delta = archive.parent / delta_name(old_version, version)
        with tempfile.TemporaryDirectory(prefix="maramax-delta-") as directory:
            changed, deleted = make_delta(old_app, destination / "Maramax.app", Path(directory))
            subprocess.run(["ditto", "-c", "-k", directory, str(delta)], check=True)
        write_checksum(delta)
        print(f"{delta} ({changed} changed, {deleted} removed since {old_version}; "
              f"{delta.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    try:
        main()
    except ReleaseError as exc:
        sys.exit(f"Not released: {exc}")
