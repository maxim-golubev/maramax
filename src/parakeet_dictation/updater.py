"""Replacing this app with a newer release published on GitHub.

Each release carries the app as a ZIP and the ZIP's SHA-256 (made by
packaging/create_release.py, uploaded by packaging/publish_release.py). The
digest catches a damaged download; what makes a download trustworthy is the
code signature, which must come from the certificate pinned below, held only
by the machine that builds releases. Nothing from a download runs, and
nothing installed is touched, until both checks pass.

A release may also carry a delta from the previous version: only the files
that changed. The app is then rebuilt from a copy of the installed one, and
the same signature check proves the result exact.

The new app is first placed beside the installed one, so that once Maramax
has quit, a small shell script only renames within that folder. It keeps
the replaced version and leaves a one-word outcome for the next launch.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import urllib.error
import urllib.request
import xml.parsers.expat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import bundle_delta
from .logger_config import logger

LATEST_RELEASE_URL = "https://api.github.com/repos/maxim-golubev/maramax/releases/latest"
# The release certificate (packaging/create_signing_identity.sh). An update
# signed with anything else is refused.
SIGNER_CERTIFICATE_SHA1 = "0A34D1F446D97A8DCD0E104D4D1528168AD83B4C"
REQUEST_TIMEOUT_SECONDS = 30
_DOWNLOAD_BLOCK = 1024 * 1024
# How long the swap waits for this process to exit before giving up.
QUIT_WAIT_SECONDS = 60
STAGED_NAME = ".Maramax-update.app"     # the new app, beside the installed one
REPLACED_NAME = ".Maramax-replaced.app"  # the old one, for the moment of the swap


class UpdateError(RuntimeError):
    pass


class InstallResult(enum.StrEnum):
    """What the swap script reports, in the file the next launch reads."""
    INSTALLED = "installed"
    NOT_QUIT = "not-quit"              # Maramax was still running after QUIT_WAIT_SECONDS
    STAGED_MISSING = "staged-missing"  # the new app was gone from beside the old one
    NOT_MOVED_ASIDE = "not-moved-aside"
    NOT_PLACED = "not-placed"          # the old app was put back


class UpdateCancelled(UpdateError):
    """The user stopped the download."""


@dataclass(frozen=True)
class UpdatePaths:
    """Where an update keeps its files under Maramax's support folder."""
    updates: Path
    log: Path

    @classmethod
    def under(cls, support_dir: Path) -> UpdatePaths:
        return cls(support_dir / "updates", support_dir / "logs" / "update.log")

    @property
    def download(self) -> Path:
        return self.updates / "download"

    @property
    def result(self) -> Path:
        """The swap script's outcome, read once by the next launch."""
        return self.updates / "last-install"

    def previous(self, installed_app: Path) -> Path:
        """The one rollback copy: the version the last update replaced."""
        return self.updates / "previous" / installed_app.name


def checksum_name(asset_name: str) -> str:
    """The name of the file holding an asset's SHA-256 (`shasum -a 256` format)."""
    return f"{asset_name}.sha256"


@dataclass(frozen=True)
class Asset:
    """One downloadable file of a release, with the URL of its SHA-256."""
    name: str
    url: str
    size: int
    checksum_url: str


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    page_url: str
    archive: Asset          # the whole app
    delta: Asset | None     # only what changed since the version installed here, when published


def version_key(version: str) -> tuple[int, ...]:
    """'0.5.2' (or a tag 'v0.5.2') as numbers, so 0.10 sorts after 0.9 and
    0.6 equals 0.6.0."""
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", version.strip())
    if match is None:
        raise UpdateError(f"Release tag {version!r} is not a version number like 0.5.2")
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _asset(tag: str, assets: dict[str, dict], name: str) -> Asset:
    archive, checksum = assets[name], assets.get(checksum_name(name))
    if checksum is None:
        raise UpdateError(f"Release {tag} has {name} without {checksum_name(name)}")
    for item in (archive, checksum):
        if item.get("state", "uploaded") != "uploaded" or not isinstance(item.get("browser_download_url"), str):
            raise UpdateError(f"Release {tag} is still being published ({item['name']} is not uploaded yet)")
    size = archive.get("size")
    if not isinstance(size, int) or size <= 0:
        raise UpdateError(f"Release {tag} does not say how large {name} is")
    return Asset(name, archive["browser_download_url"], size, checksum["browser_download_url"])


def newer_release(current_version: str, payload: dict) -> Release | None:
    """The release described by GitHub's `payload` if it is newer than
    `current_version`, else None."""
    tag = payload.get("tag_name")
    if not isinstance(tag, str):
        raise UpdateError("GitHub's latest release has no tag")
    if version_key(tag) <= version_key(current_version):
        return None
    version = tag.strip().removeprefix("v")
    assets = {asset["name"]: asset for asset in payload.get("assets", [])
              if isinstance(asset, dict) and isinstance(asset.get("name"), str)}
    archives = [name for name in assets if name.endswith(".zip")]
    if len(archives) != 1:
        raise UpdateError(f"Release {tag} should have one app .zip; it has {sorted(assets)}")
    delta_name = bundle_delta.delta_name(current_version, version)
    try:
        delta = _asset(tag, assets, delta_name) if delta_name in assets else None
    except UpdateError:
        delta = None  # A delta is only a shortcut: the whole app is always there.
    return Release(
        version=version,
        notes=str(payload.get("body") or "").strip(),
        page_url=str(payload.get("html_url", "")),
        archive=_asset(tag, assets, archives[0]),
        delta=delta,
    )


def parse_checksum(text: str) -> str:
    """The digest in a `shasum -a 256` line ('<hex>  <name>')."""
    digest = text.split(maxsplit=1)[0].lower() if text.strip() else ""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise UpdateError(f"The release's checksum file does not hold a SHA-256 digest: {text[:80]!r}")
    return digest


def signer_requirement(identifier: str) -> str:
    """The code requirement an update must satisfy (`codesign -R`)."""
    return f'identifier "{identifier}" and certificate leaf = H"{SIGNER_CERTIFICATE_SHA1}"'


def _open(url: str, current_version: str):
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"Maramax/{current_version}",
    })
    return urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS)


def latest_release(current_version: str, url: str = LATEST_RELEASE_URL) -> Release | None:
    """Ask GitHub for the newest published release. None when it is not newer."""
    try:
        with _open(url, current_version) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise UpdateError("GitHub is limiting update checks from this network for now; try again later") from exc
        if exc.code == 404:
            raise UpdateError(f"GitHub has no published release at {url}") from exc
        raise UpdateError(f"GitHub answered the update check with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"Could not reach GitHub to check for updates: {exc}") from exc
    except ValueError as exc:
        raise UpdateError(f"GitHub's answer to the update check was not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise UpdateError("GitHub's answer to the update check was not a release")
    return newer_release(current_version, payload)


def ensure_installable(installed_app: Path, updates_dir: Path) -> None:
    """Refuse, before anything is downloaded, an install that cannot work."""
    if installed_app.resolve().is_relative_to(updates_dir.resolve()):
        raise UpdateError(f"This is the copy kept from the last update ({installed_app}). "
                          "Open the installed Maramax to update it.")
    if not os.access(installed_app.parent, os.W_OK):
        raise UpdateError(f"Maramax cannot replace itself: {installed_app.parent} is not writable "
                          "(move Maramax.app into Applications first)")


def _bundle_info(app: Path) -> dict:
    try:
        return plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException, xml.parsers.expat.ExpatError) as exc:
        raise UpdateError(f"{app} is not a readable app bundle: {exc}") from exc


Progress = Callable[[int, int], None]   # bytes received, bytes expected


def download(release: Release, current_version: str, installed_app: Path, staging: Path,
             progress: Progress, cancelled: Callable[[], bool]) -> Path:
    """Fetch and verify `release` under `staging` (only what changed, when a
    delta from this version is published; the whole app otherwise, or if the
    delta does not produce a verified app), then place the new app beside
    `installed_app`, ready for install_after_exit(). Returns where it was
    placed. Raises UpdateCancelled once `cancelled()` is true. Nothing is
    left in `staging` when it fails."""
    try:
        new_app = None
        if release.delta is not None:
            try:
                _reset(staging)
                new_app = _from_delta(release, release.delta, current_version, installed_app, staging,
                                      progress, cancelled)
            except UpdateCancelled:
                raise
            except Exception as exc:
                # A delta is only a shortcut: whatever goes wrong with it, the
                # whole app is fetched instead.
                logger.warning(f"The update delta did not produce a verified app ({exc}); downloading the whole app")
        if new_app is None:
            _reset(staging)
            new_app = _from_archive(release, current_version, installed_app, staging, progress, cancelled)
        staged = _place_beside(new_app, installed_app)
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise UpdateError(f"Could not prepare Maramax {release.version}: {exc}") from exc
    except UpdateError:
        # Reported by the caller; what was downloaded is no use any more.
        shutil.rmtree(staging, ignore_errors=True)
        raise
    # Everything needed is beside the installed app now; what is left is
    # only disk space, so a failure to remove it does not fail the update.
    shutil.rmtree(staging, onexc=lambda _function, path, exc: logger.warning(
        f"Could not remove {path} after the update was staged: {exc}"))
    return staged


def _reset(staging: Path) -> None:
    if staging.exists():
        shutil.rmtree(staging)  # An earlier, unfinished download.
    staging.mkdir(parents=True)


def _fetch(asset: Asset, current_version: str, destination: Path, progress: Progress,
           cancelled: Callable[[], bool]) -> None:
    """Download `asset` to `destination` and check it against its SHA-256."""
    try:
        with _open(asset.checksum_url, current_version) as response:
            expected = parse_checksum(response.read(4096).decode("utf-8", "replace"))
        digest = hashlib.sha256()
        received = 0
        with _open(asset.url, current_version) as response, destination.open("wb") as file:
            while block := response.read(_DOWNLOAD_BLOCK):
                if cancelled():
                    raise UpdateCancelled("The download was cancelled")
                file.write(block)
                digest.update(block)
                received += len(block)
                progress(received, asset.size)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise UpdateError(f"Could not download {asset.name}: {exc}") from exc
    if digest.hexdigest() != expected:
        raise UpdateError(f"The download of {asset.name} does not match its published SHA-256")


def _from_archive(release: Release, current_version: str, installed_app: Path, staging: Path,
                  progress: Progress, cancelled: Callable[[], bool]) -> Path:
    archive = staging / "update.zip"
    _fetch(release.archive, current_version, archive, progress, cancelled)
    unpacked = staging / "unpacked"
    _run(["ditto", "-x", "-k", str(archive), str(unpacked)], f"unpack Maramax {release.version}")
    archive.unlink()
    apps = [*unpacked.glob("*/*.app"), *unpacked.glob("*.app")]
    if len(apps) != 1:
        raise UpdateError(f"Expected one app in the Maramax {release.version} archive, found {len(apps)}")
    _verify(apps[0], installed_app, release.version)
    return apps[0]


def _from_delta(release: Release, delta: Asset, current_version: str, installed_app: Path, staging: Path,
                progress: Progress, cancelled: Callable[[], bool]) -> Path:
    """The new app rebuilt from a copy of the installed one plus the delta.
    The signature check then proves every file is what was signed: a strict
    verify fails on any sealed file that is changed, missing, or extra."""
    archive = staging / "delta.zip"
    _fetch(delta, current_version, archive, progress, cancelled)
    unpacked = staging / "delta"
    _run(["ditto", "-x", "-k", str(archive), str(unpacked)], f"unpack the Maramax {release.version} delta")
    archive.unlink()
    new_app = staging / "assembled" / installed_app.name
    new_app.parent.mkdir()
    try:
        # A clone on APFS: instant, and no extra space until files differ.
        _run(["cp", "-cR", str(installed_app), str(new_app)], f"clone {installed_app}")
    except UpdateError as exc:
        logger.info(f"{exc}; copying it instead")
        shutil.rmtree(new_app, ignore_errors=True)
        _run(["ditto", str(installed_app), str(new_app)], f"copy {installed_app} to rebuild it")
    # A copy installed from a browser download carries quarantine; the
    # update must not, or Gatekeeper would stop it at the relaunch.
    _run(["xattr", "-cr", str(new_app)], "clear the rebuilt app's extended attributes")
    try:
        bundle_delta.apply(unpacked, new_app)
    except bundle_delta.DeltaError as exc:
        raise UpdateError(str(exc)) from exc
    _verify(new_app, installed_app, release.version)
    return new_app


def _verify(new_app: Path, installed_app: Path, version: str) -> None:
    info, installed = _bundle_info(new_app), _bundle_info(installed_app)
    identifier = installed.get("CFBundleIdentifier")
    if not isinstance(identifier, str) or info.get("CFBundleIdentifier") != identifier:
        raise UpdateError(f"The downloaded app is {info.get('CFBundleIdentifier')!r}, not {identifier!r}")
    if version_key(str(info.get("CFBundleShortVersionString", ""))) != version_key(version):
        raise UpdateError(f"The downloaded app says it is version {info.get('CFBundleShortVersionString')}, "
                          f"but the release is {version}")
    _run(["codesign", "--verify", "--deep", "--strict", f"-R={signer_requirement(identifier)}", str(new_app)],
         f"confirm that Maramax {version} was signed by Maramax's release certificate")


def _place_beside(new_app: Path, installed_app: Path) -> Path:
    """Move the verified app next to the installed one, so the swap is a
    rename within one folder (and one volume) once Maramax has quit."""
    staged = installed_app.parent / STAGED_NAME
    if staged.exists():
        shutil.rmtree(staged)
    if new_app.stat().st_dev == installed_app.parent.stat().st_dev:
        new_app.rename(staged)
    else:
        _run(["ditto", str(new_app), str(staged)], f"copy the new Maramax next to {installed_app}")
    return staged


def _run(command: list[str], what: str) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"Could not {what}: {exc}") from exc
    if result.returncode != 0:
        raise UpdateError(f"Could not {what}: {result.stderr.strip() or f'exit status {result.returncode}'}")


def swap_script(*, pid: int, staged_app: Path, installed_app: Path, previous_app: Path, staging: Path,
                result_path: Path, log_path: Path) -> str:
    """The shell script run after Maramax quits. It waits for process `pid`
    to exit, renames the installed app aside and `staged_app` into its
    place (both beside each other), keeps the old app as `previous_app`,
    writes an InstallResult to `result_path`, and opens whichever app ends
    up installed. Every step is checked; a bundle without an Info.plist is
    never opened."""
    variables = "\n".join(f"{name}={shlex.quote(str(value))}" for name, value in {
        "staged": staged_app, "installed": installed_app, "previous": previous_app,
        "replaced": installed_app.parent / REPLACED_NAME, "staging": staging,
        "result": result_path, "log": log_path,
    }.items())
    r = InstallResult
    return f"""#!/bin/sh
# Written by Maramax to replace itself once it has quit.
{variables}
exec >>"$log" 2>&1
finish() {{
  echo "$1" > "$result"
  echo "$(date '+%Y-%m-%d %H:%M:%S') $1"
  if [ -f "$installed/Contents/Info.plist" ]; then open "$installed"; fi
  exit 0
}}
echo "$(date '+%Y-%m-%d %H:%M:%S') installing $staged"
waited=0
while kill -0 {pid} 2>/dev/null; do
  if [ "$waited" -ge {int(QUIT_WAIT_SECONDS * 10)} ]; then
    rm -rf "$staged"
    echo {r.NOT_QUIT} > "$result"
    echo "Maramax (pid {pid}) did not quit; nothing was changed"
    exit 0
  fi
  sleep 0.1
  waited=$((waited + 1))
done
# Let LaunchServices notice the exit before the new copy asks it to launch.
sleep 1
if [ ! -f "$staged/Contents/Info.plist" ]; then finish {r.STAGED_MISSING}; fi
rm -rf "$replaced"
if [ -e "$replaced" ] || ! mv "$installed" "$replaced"; then
  rm -rf "$staged"
  finish {r.NOT_MOVED_ASIDE}
fi
if ! mv "$staged" "$installed"; then
  mv "$replaced" "$installed"
  finish {r.NOT_PLACED}
fi
# The old version is kept for rollback; if it cannot be moved, it stays hidden beside the new one.
mkdir -p "$(dirname "$previous")"
if rm -rf "$previous" && [ ! -e "$previous" ] && mv "$replaced" "$previous"; then
  echo "the previous version is at $previous"
else
  echo "the previous version stays at $replaced"
fi
rm -rf "$staging"
finish {r.INSTALLED}
"""


def install_after_exit(*, staged_app: Path, installed_app: Path, previous_app: Path, staging: Path,
                       result_path: Path, log_path: Path, pid: int) -> None:
    """Start the swap in its own session, so it outlives this process. The
    caller quits right after; the swap waits for that."""
    script = staging / "install.sh"
    try:
        staging.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.unlink(missing_ok=True)
        script.write_text(swap_script(pid=pid, staged_app=staged_app, installed_app=installed_app,
                                      previous_app=previous_app, staging=staging, result_path=result_path,
                                      log_path=log_path))
        subprocess.Popen(["/bin/sh", str(script)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        raise UpdateError(f"Could not start the installer: {exc}") from exc


def take_install_result(result_path: Path) -> InstallResult | None:
    """The outcome the last swap left behind, once: the file is removed."""
    try:
        text = result_path.read_text().strip()
    except FileNotFoundError:
        return None
    result_path.unlink(missing_ok=True)
    try:
        return InstallResult(text)
    except ValueError as exc:
        raise UpdateError(f"The last update left an unknown result {text!r} in {result_path}") from exc
