"""Cross-process task leases within one JiuwenSwarm deployment."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import IO

import portalocker


class DesktopLease:

    def __init__(self, desktop_id: str, directory: str | None = None):
        root = (Path(directory).expanduser() if directory else Path.home() / ".jiuwenswarm" / "computer_use" / "locks")
        self.path = root / (hashlib.sha256(desktop_id.encode()).hexdigest() + ".lock")
        self._file: IO[str] | None = None

    async def acquire(self) -> None:
        if self._file is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file = self.path.open("a+", encoding="utf-8")
        try:
            while True:
                try:
                    portalocker.lock(file, portalocker.LOCK_EX | portalocker.LOCK_NB)
                    self._file = file
                    return
                except portalocker.exceptions.LockException:
                    await asyncio.sleep(0.05)
        except BaseException:
            file.close()
            raise

    def release(self) -> None:
        file, self._file = self._file, None
        if file is not None:
            try:
                portalocker.unlock(file)
            finally:
                file.close()
