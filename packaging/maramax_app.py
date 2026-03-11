# ruff: noqa: E402

from pathlib import Path
import sys


def _prepend_path(path: Path) -> None:
    value = str(path)
    if not path.exists():
        return
    if value in sys.path:
        sys.path.remove(value)
    sys.path.insert(0, value)


HERE = Path(__file__).resolve().parent
BUNDLE_LIB = HERE / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
if BUNDLE_LIB.exists():
    _prepend_path(BUNDLE_LIB / "lib-dynload")
    _prepend_path(BUNDLE_LIB)
else:
    ROOT = HERE.parent
    _prepend_path(ROOT / "src")

from parakeet_dictation.main import main


if __name__ == "__main__":
    main()
