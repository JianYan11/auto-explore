"""Advisory locks for cooperating local coordinators and their explorer children."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import fcntl


class LockBusy(RuntimeError):
    pass


class FileLock:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fd: int | None = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.close()
            raise LockBusy(f"Lock already held: {path}") from None
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.fd is not None:
            # Do not LOCK_UN: a surviving explorer inherits this open description
            # and must continue holding the device lock after coordinator death.
            os.close(self.fd)
            self.fd = None

    def __enter__(self) -> FileLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def device_lock_path(serial: str, root: Path | None = None) -> Path:
    root = root or Path.home() / ".cache" / "auto-explore" / "device-locks"
    server = os.environ.get("ADB_SERVER_SOCKET", "") or (
        "local:" + os.environ.get("ANDROID_ADB_SERVER_PORT", "5037")
    )
    key = hashlib.sha256(f"{server}\0{serial}".encode()).hexdigest()
    return root / f"{key}.lock"
