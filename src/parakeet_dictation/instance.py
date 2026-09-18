"""A process-scoped lock prevents two copies sharing hotkeys and recovery files."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return True

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        # Do not unlink: another process may already be locking this inode.
