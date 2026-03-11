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
