"""Replacing this app with a newer release published on GitHub.

Each release carries the app as a ZIP and the ZIP's SHA-256 (both made by
packaging/create_release.py and uploaded by packaging/publish_release.py).
The archive is checked against that digest and the app inside it against the
installed one (same bundle identifier, the advertised version, a valid
signature) before anything is moved. The swap itself happens after Maramax
has quit, in a small shell script, and the replaced version is kept.
"""

from __future__ import annotations

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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

LATEST_RELEASE_URL = "https://api.github.com/repos/maxim-golubev/maramax/releases/latest"
REQUEST_TIMEOUT_SECONDS = 30
_DOWNLOAD_BLOCK = 1024 * 1024
# How long the swap waits for this process to exit before giving up.
QUIT_WAIT_SECONDS = 60


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    page_url: str
    archive_url: str
    archive_size: int
    checksum_url: str


def version_key(version: str) -> tuple[int, ...]:
    """'0.5.2' (or a tag 'v0.5.2') as numbers, so 0.10 sorts after 0.9."""
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", version.strip())
    if match is None:
        raise UpdateError(f"Release tag {version!r} is not a version number like 0.5.2")
    return tuple(int(part) for part in match.group(1).split("."))


def newer_release(current_version: str, payload: dict) -> Release | None:
    """The release described by GitHub's `payload` if it is newer than
    `current_version`, else None."""
    tag = payload.get("tag_name")
    if not isinstance(tag, str):
        raise UpdateError("GitHub's latest release has no tag")
    if version_key(tag) <= version_key(current_version):
        return None
    assets = {asset["name"]: asset for asset in payload.get("assets", [])
              if isinstance(asset, dict) and isinstance(asset.get("name"), str)}
    archives = [name for name in assets if name.endswith(".zip") and f"{name}.sha256" in assets]
    if len(archives) != 1:
        raise UpdateError(f"Release {tag} should have one .zip with a matching .zip.sha256; it has {sorted(assets)}")
    archive = assets[archives[0]]
    return Release(
        version=".".join(str(part) for part in version_key(tag)),
        notes=str(payload.get("body") or "").strip(),
        page_url=str(payload.get("html_url", "")),
        archive_url=archive["browser_download_url"],
        archive_size=int(archive.get("size", 0)),
        checksum_url=assets[f"{archives[0]}.sha256"]["browser_download_url"],
    )


def parse_checksum(text: str) -> str:
    """The digest in a `shasum -a 256` line ('<hex>  <name>')."""
    digest = text.split(maxsplit=1)[0].lower() if text.strip() else ""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise UpdateError(f"The release's checksum file does not hold a SHA-256 digest: {text[:80]!r}")
    return digest


def _open(url: str, current_version: str):
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"Maramax/{current_version}",
    })
    return urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS)


def latest_release(current_version: str, url: str = LATEST_RELEASE_URL) -> Release | None:
    """Ask GitHub for the newest published release. None when there is no
    newer one (or no release at all yet)."""
    try:
        with _open(url, current_version) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None  # Nothing published yet.
        raise UpdateError(f"GitHub answered the update check with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"Could not reach GitHub to check for updates: {exc}") from exc
    except ValueError as exc:
        raise UpdateError(f"GitHub's answer to the update check was not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise UpdateError("GitHub's answer to the update check was not a release")
    return newer_release(current_version, payload)


def _bundle_info(app: Path) -> dict:
    try:
        return plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise UpdateError(f"{app} is not a readable app bundle: {exc}") from exc


def download(release: Release, current_version: str, installed_app: Path, staging: Path,
             progress: Callable[[float], None]) -> Path:
    """Fetch, verify, and unpack `release` under `staging`. Returns the new
    app, ready for install_after_exit(). `progress` receives 0.0–1.0."""
    try:
        if staging.exists():
            shutil.rmtree(staging)  # An earlier, unfinished download.
        staging.mkdir(parents=True)
        with _open(release.checksum_url, current_version) as response:
            expected = parse_checksum(response.read(4096).decode("utf-8", "replace"))
        archive = staging / "update.zip"
        digest = hashlib.sha256()
        received = 0
        with _open(release.archive_url, current_version) as response, archive.open("wb") as file:
            while block := response.read(_DOWNLOAD_BLOCK):
                file.write(block)
                digest.update(block)
                received += len(block)
                if release.archive_size > 0:
                    progress(min(1.0, received / release.archive_size))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"Could not download Maramax {release.version}: {exc}") from exc
    if digest.hexdigest() != expected:
        raise UpdateError(f"The download of Maramax {release.version} does not match its published SHA-256")

    unpacked = staging / "unpacked"
    _run(["ditto", "-x", "-k", str(archive), str(unpacked)], f"unpack Maramax {release.version}")
    archive.unlink()
    apps = [*unpacked.glob("*/*.app"), *unpacked.glob("*.app")]
    if len(apps) != 1:
        raise UpdateError(f"Expected one app in the Maramax {release.version} archive, found {len(apps)}")
    new_app = apps[0]
    info, installed = _bundle_info(new_app), _bundle_info(installed_app)
    if info.get("CFBundleIdentifier") != installed.get("CFBundleIdentifier"):
        raise UpdateError(f"The downloaded app is {info.get('CFBundleIdentifier')!r}, "
                          f"not {installed.get('CFBundleIdentifier')!r}")
    if info.get("CFBundleShortVersionString") != release.version:
        raise UpdateError(f"The downloaded app says it is version {info.get('CFBundleShortVersionString')}, "
                          f"but the release is {release.version}")
    _run(["codesign", "--verify", "--deep", "--strict", str(new_app)], f"verify the signature of Maramax {release.version}")
    return new_app


def _run(command: list[str], what: str) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f"Could not {what}: {exc}") from exc
    if result.returncode != 0:
        raise UpdateError(f"Could not {what}: {result.stderr.strip() or f'exit status {result.returncode}'}")


def swap_script(*, pid: int, new_app: Path, installed_app: Path, previous_app: Path, staging: Path,
                log_path: Path) -> str:
    """The shell script that waits for process `pid` to exit, keeps the
    installed app as `previous_app`, moves `new_app` into its place, and
    opens it. Whatever fails, an app is left at `installed_app` and opened."""
    assignments = "\n".join(f"{name}={shlex.quote(str(value))}" for name, value in {
        "new": new_app, "installed": installed_app, "previous": previous_app,
        "staging": staging, "log": log_path,
    }.items())
    return f"""#!/bin/sh
# Written by Maramax to replace itself once it has quit.
{assignments}
exec >>"$log" 2>&1
echo "$(date '+%Y-%m-%d %H:%M:%S') installing $new"
waited=0
while kill -0 {pid} 2>/dev/null; do
  if [ "$waited" -ge {QUIT_WAIT_SECONDS * 10} ]; then
    echo "Maramax (pid {pid}) did not quit; the update was not installed"
    exit 1
  fi
  sleep 0.1
  waited=$((waited + 1))
done
rm -rf "$previous"
mkdir -p "$(dirname "$previous")"
if ! mv "$installed" "$previous"; then
  echo "Could not move the installed app aside; it was left as it was"
  open "$installed"
  exit 1
fi
if mv "$new" "$installed"; then
  echo "Installed; the previous version is at $previous"
  rm -rf "$staging"
else
  echo "Could not move the new app into place; restoring the previous version"
  mv "$previous" "$installed"
fi
open "$installed"
"""


def install_after_exit(*, new_app: Path, installed_app: Path, previous_app: Path, staging: Path,
                       log_path: Path, pid: int) -> None:
    """Start the swap in its own session, so it outlives this process. The
    caller quits right after; the swap waits for that."""
    if not installed_app.parent.is_dir() or not os.access(installed_app.parent, os.W_OK):
        raise UpdateError(f"Maramax cannot replace itself: {installed_app.parent} is not writable")
    script = staging / "install.sh"
    try:
        script.write_text(swap_script(pid=pid, new_app=new_app, installed_app=installed_app,
                                      previous_app=previous_app, staging=staging, log_path=log_path))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["/bin/sh", str(script)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        raise UpdateError(f"Could not start the installer: {exc}") from exc
