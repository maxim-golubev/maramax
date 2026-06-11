from __future__ import annotations

import os
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


def ensure_ssl_certs() -> None:
    """Point OpenSSL at certifi's CA bundle when the default is unusable.

    The py2app bundle has no system cert path baked in, so
    ssl.create_default_context() raises FileNotFoundError — which breaks
    Hugging Face Hub lookups and model loading.
    """
    current = os.environ.get("SSL_CERT_FILE")
    if current and Path(current).exists():
        return
    try:
        import certifi

        cert_path = certifi.where()
    except Exception:
        return
    if Path(cert_path).exists():
        os.environ["SSL_CERT_FILE"] = cert_path


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
