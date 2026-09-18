# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""宿主机 workspace 初始化（异步盘 IO + 进程级跳过缓存）.

替代上游 DirectoryBuilder 经沙箱串行 mkdir/upload ``.workspace`` 的路径。

策略：
1. 进程内已标记就绪 / 根目录已有 ``.workspace`` marker → 直接跳过；
2. 目标目录为空 → 进程级预置模板 + 异步拷贝；
3. 目标已有部分文件 → 就地 materialize（已存在文件不覆盖）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import anyio

from jiuwenswarm.server.runtime.async_fs import (
    async_exists,
    async_is_file,
    async_iterdir,
    async_mkdir,
    async_write_text,
)

logger = logging.getLogger(__name__)

_TPL_LOCK = threading.Lock()
_TPL_CACHE: dict[str, Path] = {}
# 本进程已确认就绪的 workspace 根路径（避免反复 stat / 重复 materialize）。
_READY_ROOTS: set[str] = set()
_READY_LOCK = threading.Lock()


def _dirs_fingerprint(directories: list[dict[str, Any]]) -> str:
    """Stable-ish fingerprint of workspace schema (structure + content)."""
    h = hashlib.sha1()

    def walk(nodes: list[dict[str, Any]]) -> None:
        for node in nodes or []:
            h.update(str(node.get("name", "")).encode("utf-8", errors="replace"))
            h.update(b"\0")
            h.update(str(node.get("path", "")).encode("utf-8", errors="replace"))
            h.update(b"\0")
            h.update(b"1" if node.get("is_file") else b"0")
            content = node.get("default_content") or ""
            if not isinstance(content, str):
                content = str(content)
            h.update(content.encode("utf-8", errors="replace"))
            h.update(b"\0")
            walk(list(node.get("children") or []))

    walk(list(directories or []))
    return h.hexdigest()[:20]


def _mark_ready(root: str | Path) -> None:
    key = str(Path(root))
    with _READY_LOCK:
        _READY_ROOTS.add(key)


def _is_ready(root: str | Path) -> bool:
    key = str(Path(root))
    with _READY_LOCK:
        return key in _READY_ROOTS


def clear_workspace_ready_cache() -> None:
    """测试用：清空进程级就绪缓存。"""
    with _READY_LOCK:
        _READY_ROOTS.clear()


async def _amaterialize_nodes(
    root: Path,
    nodes: list[dict[str, Any]],
    *,
    parent: Path | None = None,
    yield_every: int = 8,
) -> None:
    """按 DirectoryBuilder 语义异步落盘；周期性让出事件循环。"""
    ops = 0
    for node in nodes or []:
        relative_path = node.get("path", "") or ""
        if parent is not None:
            full_path = parent / relative_path if relative_path else parent
        else:
            full_path = root / relative_path if relative_path else root

        is_file = bool(node.get("is_file", False))
        if is_file:
            if not await async_exists(full_path):
                await async_mkdir(full_path.parent, parents=True, exist_ok=True)
                content = node.get("default_content") or ""
                if not isinstance(content, str):
                    content = str(content)
                await async_write_text(full_path, content)
                ops += 1
        else:
            await async_mkdir(full_path, parents=True, exist_ok=True)
            marker = full_path / ".workspace"
            if not await async_exists(marker):
                await async_write_text(marker, "")
                ops += 1
            children = list(node.get("children") or [])
            if children:
                await _amaterialize_nodes(
                    root, children, parent=full_path, yield_every=yield_every
                )
        if ops and ops % yield_every == 0:
            await asyncio.sleep(0)


def _materialize_nodes(
    root: Path,
    nodes: list[dict[str, Any]],
    *,
    parent: Path | None = None,
) -> None:
    """同步落盘（模板预热 / 兼容旧调用）。"""
    for node in nodes or []:
        relative_path = node.get("path", "") or ""
        if parent is not None:
            full_path = parent / relative_path if relative_path else parent
        else:
            full_path = root / relative_path if relative_path else root

        is_file = bool(node.get("is_file", False))
        if is_file:
            if not full_path.exists():
                full_path.parent.mkdir(parents=True, exist_ok=True)
                content = node.get("default_content") or ""
                if not isinstance(content, str):
                    content = str(content)
                full_path.write_text(content, encoding="utf-8")
        else:
            full_path.mkdir(parents=True, exist_ok=True)
            marker = full_path / ".workspace"
            if not marker.exists():
                marker.write_text("", encoding="utf-8")
            children = list(node.get("children") or [])
            if children:
                _materialize_nodes(root, children, parent=full_path)


def _ensure_workspace_template(
    language: str,
    directories: list[dict[str, Any]],
) -> Path:
    """进程内预置一份模板目录（同 schema 只建一次）。"""
    fp = _dirs_fingerprint(directories)
    lang = (language or "cn").strip() or "cn"
    key = f"{lang}:{fp}"
    with _TPL_LOCK:
        cached = _TPL_CACHE.get(key)
        if cached is not None and (cached / ".workspace").is_file():
            return cached
        _t0 = time.monotonic()
        base = Path(tempfile.gettempdir()) / "jiuwenswarm_ws_tpl" / key
        root_marker = base / ".workspace"
        if root_marker.is_file():
            _TPL_CACHE[key] = base
            return base
        if base.exists():
            shutil.rmtree(base, ignore_errors=True)
        base.mkdir(parents=True, exist_ok=True)
        _materialize_nodes(base, directories)
        root_marker.write_text("", encoding="utf-8")
        _TPL_CACHE[key] = base
        logger.info(
            "[SandboxPerf] workspace_template ready: key=%s path=%s elapsed_ms=%.1f",
            key,
            base,
            (time.monotonic() - _t0) * 1000,
        )
        return base


async def _acopy_template_into(tpl: Path, dest: Path) -> None:
    """异步把模板内容拷进 dest。"""
    entries = await async_iterdir(tpl)
    for item in entries:
        name = item.name
        target = dest / name
        if await anyio.Path(item).is_dir():
            await async_mkdir(target, parents=True, exist_ok=True)
            await _acopy_template_into(Path(item), target)
        else:
            data = await anyio.Path(item).read_bytes()
            await anyio.Path(target).write_bytes(data)
        await asyncio.sleep(0)


def _copy_template_into(tpl: Path, dest: Path) -> None:
    """把模板内容拷进空的 dest（dest 已存在且应为空）。"""
    for item in tpl.iterdir():
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def host_init_workspace_sync(
    root: str,
    directories: list[dict[str, Any]] | None,
    *,
    language: str = "cn",
) -> dict[str, Any]:
    """同步初始化宿主机 workspace（兼容旧调用 / 测试）。"""
    root_path = Path(root)
    if _is_ready(root_path):
        return {"status": "skipped_ready_cache", "path": str(root_path)}
    marker = root_path / ".workspace"
    if marker.exists():
        _mark_ready(root_path)
        return {"status": "skipped_marker", "path": str(root_path)}

    dirs = list(directories or [])
    root_path.mkdir(parents=True, exist_ok=True)
    try:
        is_empty = not any(root_path.iterdir())
    except OSError:
        is_empty = False

    if is_empty and dirs:
        tpl = _ensure_workspace_template(language, dirs)
        _copy_template_into(tpl, root_path)
        status = "copytree"
    else:
        if dirs:
            _materialize_nodes(root_path, dirs)
        status = "materialize"

    if not marker.exists():
        marker.write_text("", encoding="utf-8")
    _mark_ready(root_path)
    return {
        "status": status,
        "path": str(root_path),
        "dirs": len(dirs),
    }


async def host_init_workspace(
    root: str,
    directories: list[dict[str, Any]] | None,
    *,
    language: str = "cn",
) -> dict[str, Any]:
    """异步初始化宿主机 workspace（推荐入口）。"""
    root_path = Path(root)
    if _is_ready(root_path):
        return {"status": "skipped_ready_cache", "path": str(root_path)}
    marker = root_path / ".workspace"
    if await async_exists(marker):
        _mark_ready(root_path)
        return {"status": "skipped_marker", "path": str(root_path)}

    dirs = list(directories or [])
    await async_mkdir(root_path, parents=True, exist_ok=True)
    try:
        entries = await async_iterdir(root_path)
        is_empty = not entries
    except OSError:
        is_empty = False

    if is_empty and dirs:
        tpl = _ensure_workspace_template(language, dirs)
        await _acopy_template_into(tpl, root_path)
        status = "copytree"
    else:
        if dirs:
            await _amaterialize_nodes(root_path, dirs)
        status = "materialize"

    if not await async_exists(marker):
        await async_write_text(marker, "")
    _mark_ready(root_path)
    return {
        "status": status,
        "path": str(root_path),
        "dirs": len(dirs),
    }


__all__ = [
    "clear_workspace_ready_cache",
    "host_init_workspace",
    "host_init_workspace_sync",
]
