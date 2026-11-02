# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Flash Bash 删除探测的双账本适配层。

stock ``BashTool`` 的删除兜底只读取 ``file_ops_*.json``；flash edit 的
最新内容则在 ``file_ops_flash_*.jsonl``。这里将两种账本合并后按时间取
“最新有效条目”，再把缺失的 delete 记录追加回 flash JSONL：

- flash-only 历史：不再因为 stock 账本为空而漏记；
- stock + flash 混合历史：不会拿 stock 里的旧 ``new_content`` 当删除前内容；
- 显式 ``rm`` 目标：命令执行前读取当前文件内容，保证即使文件从未入账
  也能记录删除前全文。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.sys_operation.cwd import get_cwd

from .flash_file_history import (
    _history_lock,
    _rewrite_flash_history_unlocked,
    append_flash_history_entry,
    load_flash_history,
)


logger = logging.getLogger(__name__)

# ``rewound_out`` 只回退对话可见性，工作区文件仍保留该次编辑的结果；只有
# ``discarded_out`` 表示 discard 已经把工作区恢复到旧版本。删除探测关注
# 磁盘当前内容，因此不能把 conversation rewind 的条目排除掉。
_DISCARDED_OUT_KEY = "discarded_out"


def _load_stock_history(history_path: Optional[str]) -> Dict[str, List[Dict[str, Any]]]:
    if not history_path:
        return {}
    path = pathlib.Path(history_path)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "[flash bash history] failed to read stock history %s: %s", path, exc
        )
        return {}
    if not isinstance(raw, dict):
        return {}

    result: Dict[str, List[Dict[str, Any]]] = {}
    for file_path, entries in raw.items():
        if not isinstance(file_path, str) or not isinstance(entries, list):
            continue
        valid_entries = [entry for entry in entries if isinstance(entry, dict)]
        if valid_entries:
            result[file_path] = valid_entries
    return result


def _path_key(file_path: str) -> str:
    try:
        return os.path.normcase(str(pathlib.Path(file_path).resolve()))
    except OSError:
        return os.path.normcase(os.path.abspath(file_path))


def _entry_sort_ts(entry: Dict[str, Any]) -> float:
    """时间戳排序键；无法解析的条目不参与“最新内容”竞争。"""
    ts = entry.get("timestamp")
    if not isinstance(ts, str) or not ts:
        return float("-inf")
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _latest_effective_entry(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """返回能代表当前磁盘状态的最新条目。

    ``rewound_out`` 只影响对话可见性，工作区仍保留该次写入，因此按普通条目
    参与重放。``discarded_out`` 表示 discard 已经把该时间戳之后的写入恢复到
    本轮开始前；如果被丢弃的是文件的全部历史（例如文件首次被 agent 修改后
    又被 discard），不能因为没有更早的有效条目而丢失删除前内容，应取首个
    discarded 条目的 ``old_content``。
    """
    if not entries:
        return None

    latest = max(entries, key=_entry_sort_ts)
    if not latest.get(_DISCARDED_OUT_KEY):
        return latest

    non_discarded = [
        entry for entry in entries if not entry.get(_DISCARDED_OUT_KEY)
    ]
    baseline_ts = (
        max(_entry_sort_ts(entry) for entry in non_discarded)
        if non_discarded
        else float("-inf")
    )
    discarded_suffix = [
        entry
        for entry in entries
        if entry.get(_DISCARDED_OUT_KEY) and _entry_sort_ts(entry) > baseline_ts
    ]
    if not discarded_suffix:
        return None

    restored = min(discarded_suffix, key=_entry_sort_ts)
    old_content = restored.get("old_content")
    if old_content is None:
        # 文件在被丢弃的轮次开始前不存在；discard 已经把它删除。
        return {"action": "delete", "new_content": None}
    return {"action": "edit", "new_content": old_content}


def _merged_latest_by_path(
    stock_data: Dict[str, List[Dict[str, Any]]],
    flash_data: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Dict[str, Any]]:
    """按规范化路径合并两种账本；代表路径优先使用 flash 侧路径。"""
    merged: Dict[str, Dict[str, Any]] = {}
    for source in (flash_data, stock_data):
        for file_path, entries in source.items():
            key = _path_key(file_path)
            item = merged.setdefault(key, {"path": file_path, "entries": []})
            if source is flash_data:
                item["path"] = file_path
            item["entries"].extend(entries)
    return merged


def record_detected_deletions(
    stock_history_path: Optional[str],
    flash_history_path: Optional[str],
) -> None:
    """命令成功后扫描双账本，把消失文件的最新内容记为 delete。

    读改写全程持有 flash per-path 锁，避免与 flash edit 的追加或其他
    rewrite 交错覆盖。stock 账本只读，不改变其内容；diff_service 后续会
    按时间戳把 flash 侧 delete 与 stock/flash 编辑合并。
    """
    if not flash_history_path:
        return
    flash_path = pathlib.Path(flash_history_path)
    try:
        with _history_lock(str(flash_path)):
            flash_data = load_flash_history(flash_path)
            stock_data = _load_stock_history(stock_history_path)
            merged = _merged_latest_by_path(stock_data, flash_data)
            now = datetime.now(timezone.utc).isoformat()
            changed = False

            for item in merged.values():
                latest = _latest_effective_entry(item["entries"])
                if latest is None or latest.get("action") == "delete":
                    continue
                file_path = item["path"]
                if os.path.exists(file_path):
                    continue
                flash_data.setdefault(file_path, []).append(
                    {
                        "action": "delete",
                        "timestamp": now,
                        "old_content": latest.get("new_content"),
                        "new_content": None,
                    }
                )
                changed = True

            if changed:
                _rewrite_flash_history_unlocked(flash_path, flash_data)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[flash bash history] failed to record deletions (%s, %s): %s",
            stock_history_path,
            flash_history_path,
            exc,
        )


async def record_rm_targets_before_deletion(
    flash_history_path: Optional[str],
    file_paths: List[str],
    operation: Any,
) -> None:
    """显式 rm 目标执行前读取当前全文并追加 delete 记录。"""
    if not flash_history_path or not file_paths:
        return
    cwd = get_cwd()
    for raw_path in file_paths:
        try:
            expanded = pathlib.Path(raw_path).expanduser()
            abs_path = str(
                (pathlib.Path(cwd) / expanded).resolve()
                if not expanded.is_absolute()
                else expanded.resolve()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[flash bash history] path resolve failed %s: %s", raw_path, exc
            )
            continue
        if not os.path.isfile(abs_path):
            continue
        try:
            res = await operation.fs().read_file(abs_path)
            if res.code != StatusCode.SUCCESS.code:
                continue
            content = res.data.content if res.data else ""
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            entry = {
                "action": "delete",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "old_content": content,
                "new_content": None,
            }
            await asyncio.to_thread(
                append_flash_history_entry, flash_history_path, abs_path, entry
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[flash bash history] failed to record rm target %s: %s",
                abs_path,
                exc,
            )


__all__ = [
    "record_detected_deletions",
    "record_rm_targets_before_deletion",
]
