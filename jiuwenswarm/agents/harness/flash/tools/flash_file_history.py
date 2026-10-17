# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""flash 文件操作的 JSONL history（热路径纯追加 + 低频原子重写）。

stock 的 ``_append_op_history``（agent-core，冻结）每次操作都要
json.load 整个历史 → append → json.dump 整文件原子替换，单次成本
O(全部历史)，长会话内随编辑次数二次方恶化。flash edit_file 改为独立
JSONL 账本：

- **热路径（每次编辑）**：``append_flash_history_entry`` 纯追加一行，
  O(单条目)，不触碰旧行；
- **低频路径（/rewind 软删 / restore / discard）**：diff_service 调
  ``rewrite_flash_history`` 原子重写——语义与 stock 的整文件重写完全
  一致，只是把重写成本从"每次编辑"挪到"每次回退"（用户触发的稀有
  操作）。

条目 schema 与 stock JSON 一致（``{action, timestamp, old_content,
new_content}`` + 软删标记字段），差异仅在文件形状：JSONL 每行一条并
额外携带 ``file_path`` 分组键。``load_flash_history`` 聚合后即为 stock
的 ``{file_path: [entries]}`` dict，diff_service 的合并/去重/回滚/软删
逻辑零改动复用。

文件名约定 ``file_ops_flash_{agent_id}_{session_id}.jsonl``：以
``file_ops_`` 开头保证 cleanup 扫描与 diff_service 的 session 匹配可
及；``.jsonl`` 后缀保证不会被 stock 的 ``.json`` 精确匹配误当成 JSON
解析。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Union

from openjiuwen.core.sys_operation.cwd import get_cwd, get_workspace

# 进程内互斥：append（flash 工具，async 侧经 to_thread 调入）与 rewrite
# （diff_service，同步调用）共用同一把 per-path 锁，保证追加与重写不交错。
# stock 只用 asyncio.Lock 护自己的写入，同样不跨进程——这里与其风险等级
# 对齐，不引入文件锁。
_HISTORY_LOCKS: Dict[str, threading.Lock] = {}
_HISTORY_LOCKS_GUARD = threading.Lock()

FLASH_HISTORY_SUFFIX = ".jsonl"


def _history_lock(history_path: str) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(history_path))
    with _HISTORY_LOCKS_GUARD:
        lock = _HISTORY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _HISTORY_LOCKS[key] = lock
        return lock


def build_flash_history_path(session: Any) -> Optional[str]:
    """与 stock EditFileTool._build_history_path 同口径的落点推导。

    stock edit 在 flash 会话里写的就是这个 base_dir（get_workspace() 或
    cwd）下的 .agent_history，diff_service 也从这里扫描——flash 账本落在
    同一目录才能被合并读取。
    """
    try:
        base_dir = get_workspace() or str(Path(get_cwd()).expanduser().resolve())
        agent_id = (
            session.agent_id() if hasattr(session, "agent_id")
            else session.get_agent_id() if hasattr(session, "get_agent_id")
            else None
        ) or "default"
        return os.path.join(
            base_dir, ".agent_history",
            f"file_ops_flash_{agent_id}_{session.get_session_id()}.jsonl",
        )
    except Exception:
        return None


def append_flash_history_entry(
    history_path: str,
    file_path: str,
    entry: Dict[str, Any],
) -> None:
    """追加一条 file_ops 记录（同步函数；调用方负责 to_thread）。"""
    line = {"file_path": file_path, **entry}
    payload = json.dumps(line, ensure_ascii=False) + "\n"
    with _history_lock(history_path):
        os.makedirs(os.path.dirname(history_path), exist_ok=True)
        with open(history_path, "a", encoding="utf-8") as f:
            f.write(payload)


def load_flash_history(history_path: Union[str, Path]) -> Dict[str, list]:
    """JSONL → {file_path: [entries]}（stock JSON 的聚合形状）。"""
    result: Dict[str, list] = {}
    path = Path(history_path)
    if not path.is_file():
        return result
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # 单行损坏只丢该行，不拖垮整个历史（stock 对整个 JSON
                    # 解析失败的容忍度是丢整个文件，这里逐行更细）。
                    continue
                if not isinstance(record, dict):
                    continue
                file_path = record.get("file_path")
                if not isinstance(file_path, str) or not file_path:
                    continue
                entry = {k: v for k, v in record.items() if k != "file_path"}
                result.setdefault(file_path, []).append(entry)
    except OSError:
        return {}
    return result


def _rewrite_flash_history_unlocked(path: Path, data: Dict[str, Any]) -> None:
    lines = []
    for file_path, entries in data.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            lines.append(
                json.dumps({"file_path": file_path, **entry}, ensure_ascii=False)
            )
    path_str = str(path)
    os.makedirs(os.path.dirname(path_str), exist_ok=True)
    tmp = path_str + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    os.replace(tmp, path_str)


def rewrite_flash_history(history_path: Union[str, Path], data: Dict[str, Any]) -> None:
    """按 stock 形状原子重写 JSONL（软删/恢复/截断专用，低频路径）。"""
    path = Path(history_path)
    with _history_lock(str(path)):
        _rewrite_flash_history_unlocked(path, data)


def is_flash_history_name(name: str) -> bool:
    return name.startswith("file_ops_flash_") and name.endswith(FLASH_HISTORY_SUFFIX)


__all__ = [
    "FLASH_HISTORY_SUFFIX",
    "append_flash_history_entry",
    "build_flash_history_path",
    "is_flash_history_name",
    "load_flash_history",
    "rewrite_flash_history",
]
