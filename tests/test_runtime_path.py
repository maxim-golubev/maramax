import os
from pathlib import Path

from parakeet_dictation.paths import RUNTIME_BIN_CANDIDATES, ensure_runtime_path


def test_ensure_runtime_path_adds_homebrew_and_bundle_bin(monkeypatch, tmp_path):
    bundle_root = tmp_path / "bundle"
    bundle_bin = bundle_root / "bin"
    bundle_bin.mkdir(parents=True)

    monkeypatch.setenv("RESOURCEPATH", str(bundle_root))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    updated = ensure_runtime_path().split(os.pathsep)

    assert updated[0] == str(bundle_bin)
    assert updated.count("/usr/bin") == 1
    for candidate in RUNTIME_BIN_CANDIDATES[1:]:
        if Path(candidate).is_dir():
            assert candidate in updated
