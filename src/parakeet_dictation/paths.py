"""Where things are on this machine: the app bundle and its resources, the data directory, command-line tools, CA certificates."""

from __future__ import annotations

import os
import plistlib
from pathlib import Path


RUNTIME_BIN_CANDIDATES = (
    "bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


def app_support_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Maramax"


def app_bundle() -> Path | None:
    """The .app this process runs from (RESOURCEPATH is its Contents/Resources),
    or None when it runs from source."""
    resources = os.getenv("RESOURCEPATH")
    return Path(resources).parents[1] if resources else None


def bundle_identifier() -> str | None:
    """The CFBundleIdentifier of the .app this process runs from, or None when it runs from source."""
    bundle = app_bundle()
    if bundle is None:
        return None
    with open(bundle / "Contents" / "Info.plist", "rb") as info:
        return plistlib.load(info)["CFBundleIdentifier"]


def ensure_ssl_certs() -> None:
    """Verify TLS connections (the update check, model downloads) against
    macOS's own trust store, which macOS keeps current: a CA list frozen
    into the bundle goes stale over the years one version runs unchanged.

    certifi's bundle stays as SSL_CERT_FILE for whatever reads that variable
    itself: the py2app bundle has no system cert path baked in, so without
    it ssl.create_default_context() raises FileNotFoundError.
    """
    # Bundled and checked by the build: a missing package fails here, at
    # launch, not later as a TLS error in an update check or model download.
    import certifi
    import truststore

    current = os.environ.get("SSL_CERT_FILE")
    if not (current and Path(current).exists()):
        cert_path = certifi.where()
        if not Path(cert_path).exists():
            raise RuntimeError(f"certifi's CA bundle is missing: {cert_path}")
        os.environ["SSL_CERT_FILE"] = cert_path
    truststore.inject_into_ssl()


def resource_path(*parts: str) -> Path:
    bundle_root = os.getenv("RESOURCEPATH")
    candidates = []
    if bundle_root:
        candidates.append(Path(bundle_root))

    candidates.append(Path(__file__).resolve().parents[2])

    for base in candidates:
        candidate = base.joinpath(*parts)
        if candidate.exists():
            return candidate

    return candidates[0].joinpath(*parts)


def ensure_runtime_path() -> str:
    path_entries = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry]
    resource_root = os.getenv("RESOURCEPATH")
    candidates: list[str] = []

    for candidate in RUNTIME_BIN_CANDIDATES:
        if candidate == "bin":
            if not resource_root:
                continue
            candidate_path = Path(resource_root) / candidate
        else:
            candidate_path = Path(candidate)

        if not candidate_path.is_dir():
            continue

        resolved = str(candidate_path)
        if resolved not in candidates:
            candidates.append(resolved)

    combined = candidates + [entry for entry in path_entries if entry not in candidates]
    os.environ["PATH"] = os.pathsep.join(combined)
    return os.environ["PATH"]
