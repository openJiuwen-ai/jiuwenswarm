# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""轻量异步文件读写（基于 anyio，不依赖 aiofiles / to_thread）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio


async def async_read_text(path: str | Path, *, encoding: str = "utf-8") -> str:
    return await anyio.Path(path).read_text(encoding=encoding)


async def async_write_text(
    path: str | Path,
    text: str,
    *,
    encoding: str = "utf-8",
) -> None:
    p = anyio.Path(path)
    await p.parent.mkdir(parents=True, exist_ok=True)
    await p.write_text(text, encoding=encoding)


async def async_mkdir(path: str | Path, *, parents: bool = True, exist_ok: bool = True) -> None:
    await anyio.Path(path).mkdir(parents=parents, exist_ok=exist_ok)


async def async_exists(path: str | Path) -> bool:
    return await anyio.Path(path).exists()


async def async_is_file(path: str | Path) -> bool:
    return await anyio.Path(path).is_file()


async def async_iterdir(path: str | Path) -> list[Any]:
    entries: list[Any] = []
    async for item in anyio.Path(path).iterdir():
        entries.append(item)
    return entries


__all__ = [
    "async_exists",
    "async_is_file",
    "async_iterdir",
    "async_mkdir",
    "async_read_text",
    "async_write_text",
]
