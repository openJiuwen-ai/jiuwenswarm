# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""session.prepare 各会话走到了哪一步，供对话首次取实例时判定命中结果。"""
# TenantAgentPool 写排队 / 构建 / 失败 / 移出与对话加入的等待，会话实例回收时写 reclaimed；
# deep 适配器在对话首次取得实例时取走并记一条命中日志。进程内、有上限。

from __future__ import annotations

from collections import OrderedDict
from typing import Any

MAX_TRACKED_SESSIONS = 1024

_entries: OrderedDict[str, dict[str, Any]] = OrderedDict()

# 未命中原因：对话新建实例时，按该会话准备的最后状态给出。
_MISS_REASONS = {
    "queued": "cancelled_in_queue",
    "dropped": "cancelled_in_queue",
    "queue_full": "queue_full",
    "failed": "prepare_failed",
    "reclaimed": "reclaimed",
}


def mark(session_id: str, state: str) -> None:
    entry = _entries.pop(session_id, {})
    entry["state"] = state
    _entries[session_id] = entry
    while len(_entries) > MAX_TRACKED_SESSIONS:
        _entries.popitem(last=False)


def advance(session_id: str, state: str) -> bool:
    entry = _entries.get(session_id)
    if entry is None:
        return False
    entry["state"] = state
    return True


def mark_joined(session_id: str, wait_ms: float) -> None:
    entry = _entries.get(session_id)
    if entry is not None:
        entry["join_wait_ms"] = wait_ms


def is_built(session_id: str) -> bool:
    """准备出的实例已建好（或正在收尾），且还没有对话为它记过命中结果。"""
    entry = _entries.get(session_id)
    return entry is not None and entry.get("state") in {"building", "ready"}


def take(session_id: str) -> dict[str, Any] | None:
    return _entries.pop(session_id, None)


def miss_reason(entry: dict[str, Any] | None) -> str:
    if entry is None:
        return "not_prepared"
    state = str(entry.get("state") or "")
    # building / ready 却要新建：实例在对话前被别的路径移除（如企业版按身份重建）。
    return _MISS_REASONS.get(state, f"lost_after_{state or 'unknown'}")


def clear() -> None:
    _entries.clear()
