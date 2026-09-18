# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""按 path+mtime 缓存的 YAML/文本读盘（同步热路径用，避免反复 safe_load）。"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml

_LOCK = threading.Lock()
_YAML_CACHE: dict[str, tuple[tuple[int, int] | None, Any]] = {}


def _file_identity(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def load_yaml_file_cached(path: str | Path) -> dict[str, Any]:
    """读 YAML；同一文件 mtime+size 未变则返回缓存副本。

    文件不存在返回 ``{}``（与 RuntimePromptRail 旧行为一致）。
    """
    p = Path(path)
    key = str(p)
    identity = _file_identity(p)
    with _LOCK:
        hit = _YAML_CACHE.get(key)
        if hit is not None and hit[0] == identity and identity is not None:
            cached = hit[1]
            return dict(cached) if isinstance(cached, dict) else cached

    if identity is None:
        with _LOCK:
            _YAML_CACHE[key] = (None, {})
        return {}

    try:
        with open(p, encoding="utf-8") as f:
            parsed = yaml.safe_load(f) or {}
    except FileNotFoundError:
        parsed = {}
    except Exception:
        raise

    if not isinstance(parsed, dict):
        parsed = {}
    with _LOCK:
        _YAML_CACHE[key] = (identity, dict(parsed))
    return dict(parsed)


def clear_yaml_file_cache() -> None:
    """测试用。"""
    with _LOCK:
        _YAML_CACHE.clear()


__all__ = [
    "clear_yaml_file_cache",
    "load_yaml_file_cached",
]
