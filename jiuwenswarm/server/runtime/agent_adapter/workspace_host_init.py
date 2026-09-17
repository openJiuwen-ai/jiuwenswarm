# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""宿主机 workspace 初始化（纯盘 IO，供 ``asyncio.to_thread`` 调用）.

替代上游 DirectoryBuilder 经沙箱串行 mkdir/upload ``.workspace`` 的路径。
本模块函数本身是同步的；调用方应丢到线程池，不要直接堵事件循环。

策略：
1. 根目录已有 ``.workspace`` marker → 直接跳过；
2. 目标目录为空 → 进程级预置模板 + ``copytree``；
3. 目标已有部分文件 → 就地 materialize（已存在文件不覆盖）。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TPL_LOCK = threading.Lock()
_TPL_CACHE: dict[str, Path] = {}


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
            # 必须哈希正文，否则同长度改文案会命中脏模板。
            h.update(content.encode("utf-8", errors="replace"))
            h.update(b"\0")
            walk(list(node.get("children") or []))

    walk(list(directories or []))
    return h.hexdigest()[:20]


def _materialize_nodes(
    root: Path,
    nodes: list[dict[str, Any]],
    *,
    parent: Path | None = None,
) -> None:
    """按 DirectoryBuilder 语义落盘：目录写 ``.workspace``，文件不存在才写。"""
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
            "[SandboxPerf] workspace_template ready: key=%s path=%s",
            key,
            base,
        )
        return base


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
    """同步初始化宿主机 workspace。

    Returns:
        状态字典：``status`` 为 skipped_marker / copytree / materialize。
    """
    root_path = Path(root)
    marker = root_path / ".workspace"
    if marker.exists():
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
    return {
        "status": status,
        "path": str(root_path),
        "dirs": len(dirs),
    }
