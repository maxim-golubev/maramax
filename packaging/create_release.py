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
# The app's own rules: what the updater accepts, and the delta format it reads.
from parakeet_dictation import bundle_delta  # noqa: E402
from parakeet_dictation.updater import UpdateError, checksum_name, signer_requirement, version_key  # noqa: E402

RELEASE_SIGNING = "Maramax release certificate"  # build-info.json's mark of a build installed copies accept
BUILD_STAMP = "build-stamp.json"  # written beside the bundle by build_app.sh: the commit it was built from


class ReleaseError(RuntimeError):
    pass


def release_paths(root: Path, version: str) -> tuple[Path, Path, Path]:
    """Where a release's folder, ZIP, and ZIP checksum are written."""
    destination = root / "releases" / f"Maramax-{version}"
    archive = destination.parent / f"{destination.name}.zip"
    return destination, archive, checksum_path(archive)


def assets_path(root: Path, version: str) -> Path:
    """The list of what publish_release.py uploads, with each file's SHA-256."""
    return root / "releases" / f"Maramax-{version}.assets.json"


def published_path(root: Path, version: str) -> Path:
    """Written by publish_release.py once GitHub has the release: installed
    copies may be at this version, so a later release's delta can start here."""
    return root / "releases" / f"Maramax-{version}.published"


def checksum_path(asset: Path) -> Path:
    return asset.with_name(checksum_name(asset.name))


def previous_release(root: Path, version: str) -> tuple[str, Path] | None:
    """The newest earlier release published from this machine and signed
    with the release certificate: copies of it can update with a delta. One
    built but never published is skipped, as no copy can be at it."""
    found = []
    for info in (root / "releases").glob("Maramax-*/build-info.json"):
        try:
            build = json.loads(info.read_text())
            built = str(build["version"])
            key = version_key(built)
        except (OSError, ValueError, KeyError, TypeError, UpdateError) as exc:
            raise ReleaseError(f"{info} cannot be read: {exc}") from exc
        if (build.get("signing") == RELEASE_SIGNING and (info.parent / "Maramax.app").is_dir()
                and published_path(root, built).is_file() and key < version_key(version)):
            found.append((key, built, info.parent / "Maramax.app"))
    return max(found)[1:] if found else None


def built_dirty(stamp: Path, head: str) -> bool:
    """Whether the bundle was built from a checkout with uncommitted changes,
    as build_app.sh recorded in `stamp`. Refuses a bundle built from another
    commit than `head`, whichever of its files changed since."""
    try:
        recorded = json.loads(stamp.read_text())
        commit, dirty = recorded["commit"], recorded["dirty"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ReleaseError(f"{stamp} cannot be read ({exc}); build the app with build_app.sh") from exc
    if not isinstance(commit, str) or not isinstance(dirty, bool):
        raise ReleaseError(f"{stamp} does not hold a commit and a dirty flag; build the app with build_app.sh")
    if commit != head:
        raise ReleaseError(f"Stale bundle: it was built from {commit}, but HEAD is {head}; run build_app.sh")
    return dirty


def write_checksum(asset: Path) -> str:
    with asset.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    checksum_path(asset).write_text(f"{digest}  {asset.name}\n")
    return digest


def write_delta(old_version: str, old_app: Path, new_app: Path, delta: Path, identifier: str) -> str:
    """Make the delta from `old_app` and prove it before it is published: a
    clone of the old app plus the delta must be exactly the new app and
    carry the release signature, as an installed copy will check."""
    with tempfile.TemporaryDirectory(prefix="maramax-delta-") as directory:
        work = Path(directory)
        changed, deleted = bundle_delta.make(old_app, new_app, work / "delta")
        rebuilt = work / "rebuilt" / new_app.name
        rebuilt.parent.mkdir()
        subprocess.run(["cp", "-cR", str(old_app), str(rebuilt)], check=True)
        try:
            bundle_delta.apply(work / "delta", rebuilt)
        except bundle_delta.DeltaError as exc:
            raise ReleaseError(f"The delta from {old_version} does not rebuild this release: {exc}") from exc
        if subprocess.run(["codesign", "--verify", "--deep", "--strict", f"-R={signer_requirement(identifier)}",
                           str(rebuilt)]).returncode != 0:
            raise ReleaseError(f"The app rebuilt from {old_version} and the delta fails the signature check")
        subprocess.run(["ditto", "-c", "-k", str(work / "delta"), str(delta)], check=True)
    print(f"{delta} ({changed} changed, {deleted} removed since {old_version}; "
          f"{delta.stat().st_size / 1e6:.1f} MB; checked by rebuilding)")
    return write_checksum(delta)


def main() -> None:
    root = ROOT
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    bundle = root / "dist" / "Maramax.app"
    destination, archive, _ = release_paths(root, version)
    if destination.exists():
        # Previous releases are never overwritten; a half-made one from a
        # failed run has to be removed by hand, deliberately.
        raise ReleaseError(f"{destination} already exists; remove it (and its .zip, deltas, and .assets.json) "
                           f"to release {version} again")
    for stale in (*destination.parent.glob(bundle_delta.delta_glob(version)), assets_path(root, version)):
        stale.unlink(missing_ok=True)  # From a run that stopped before its folder was made: not this build's.
        checksum_path(stale).unlink(missing_ok=True)
    # Before anything is written, so an unreadable old release stops this one cleanly.
    previous = previous_release(root, version)

    info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    if info["CFBundleShortVersionString"] != version:
        raise ReleaseError(f"Bundle is version {info['CFBundleShortVersionString']}, pyproject.toml says {version}")
    # Installed copies accept only an update signed with the release certificate.
    signed = subprocess.run(["codesign", "--verify", "--deep", "--strict",
                             f"-R={signer_requirement(info['CFBundleIdentifier'])}", str(bundle)])
    if signed.returncode != 0:
        raise ReleaseError("The bundle is not signed with the release certificate; build it on the machine "
                           "that holds the signing keychain (packaging/create_signing_identity.sh)")
    # --version exits before creating the GUI, models, or an audio backend.
    output = subprocess.check_output([str(bundle / "Contents" / "MacOS" / "Maramax"), "--version"], text=True)
    if output.strip() != f"maramax {version}":
        raise ReleaseError(f"Bundle reports {output.strip()!r}, expected 'maramax {version}'")
    # The commit and the dirty flag are the build's, not today's checkout's.
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    dirty = built_dirty(bundle.parent / BUILD_STAMP, commit)
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
    # The guide the bundle was built with, whatever the checkout holds now.
    shutil.copyfile(resources / "LAUNCH.md", destination / "START HERE.md")
    # publish_release.py publishes only a build of a clean, pushed commit.
    (destination / "build-info.json").write_text(json.dumps({
        "version": version, "architecture": "arm64", "signing": RELEASE_SIGNING,
        "commit": commit, "dirty": dirty, "source_sha256": hashes,
    }, indent=2) + "\n")
    subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(destination), str(archive)], check=True)
    assets = {archive.name: write_checksum(archive)}
    print(destination)
    print(archive)

    if previous is None:
        print("No delta: no earlier release is marked published here (publish_release.py marks each one)")
    else:
        old_version, old_app = previous
        delta = archive.parent / bundle_delta.delta_name(old_version, version)
        assets[delta.name] = write_delta(old_version, old_app, destination / "Maramax.app", delta,
                                         info["CFBundleIdentifier"])
    # Exactly these files, with these digests, are what publish_release.py uploads.
    assets_path(root, version).write_text(json.dumps(assets, indent=2) + "\n")


if __name__ == "__main__":
    try:
        main()
    except ReleaseError as exc:
        sys.exit(f"Not released: {exc}")
