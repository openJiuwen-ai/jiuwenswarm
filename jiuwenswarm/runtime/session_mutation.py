# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cross-process serialization for durable Session mutations."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, BinaryIO

_LOCK_FILE_NAME = ".runtime-mutation.lock"
_LOCK_POLL_SECONDS = 0.05


def _try_lock(path: Path) -> BinaryIO | None:
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                handle.close()
                return None
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                return None
        return handle
    except BaseException:
        handle.close()
        raise


def _unlock(handle: BinaryIO) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@asynccontextmanager
async def session_mutation_lock(session_dir: Path) -> AsyncIterator[None]:
    """Hold one OS-backed lock without blocking the event loop while waiting."""
    lock_path = session_dir / _LOCK_FILE_NAME
    handle: BinaryIO | None = None
    try:
        while handle is None:
            handle = _try_lock(lock_path)
            if handle is None:
                await asyncio.sleep(_LOCK_POLL_SECONDS)
        yield
    finally:
        if handle is not None:
            _unlock(handle)


__all__ = ["session_mutation_lock"]
