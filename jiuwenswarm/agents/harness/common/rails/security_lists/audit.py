# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""统一安全名单审计（JSONL 落盘）。

事件类型（``kind``）：
- ``security.list.hit``：名单命中（verdict/记录/来源/模式/操作/工具/处置）；
- ``security.list.change``：名单变更（RPC 写/审批记住的条目明细）；
- ``security.list.fallback``：兜底事件（名单解析异常 DENY / CSPL 降级等）；
- ``security.fallback.switch``：兜底总开关变更（M5）；
- ``audit_start`` / ``audit_health`` / ``audit_recovered``：审计自身可靠性
  事件（M5-3，设计 5.7）。

落盘位置：``get_logs_dir() / security_audit.jsonl``，每行一个 JSON 事件。

可靠性兜底（设计 5.7）：
- 写盘异常 → 事件进内存环形缓冲（容量 1024，溢出丢最旧并计数 ``dropped``）；
- 重试为**事件驱动**：后续 ``log_event`` 调用时距上次重试 ≥30s 先尝试刷缓冲
  （无后台线程：无线程生命周期/测试隔离问题，且无事件时本就无盘可刷）；
- 缓冲全部刷出（或写盘恢复）后补写 ``audit_recovered{dropped:n}``；
- 连续失败 ≥5 或缓冲占用 ≥80% → ``audit_health=degraded``：落 ``audit_health``
  事件 + 桌面降级提示（behavior bridge 通道，best-effort）；
- ``self_check()`` 启动自检写 ``audit_start``，失败立即告警（不等首次事件）。

审计自身失败仅告警，绝不影响业务判定。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.rails.security_lists.models import utc_now_iso

logger = logging.getLogger(__name__)

AUDIT_HIT = "security.list.hit"
AUDIT_CHANGE = "security.list.change"
AUDIT_FALLBACK = "security.list.fallback"
AUDIT_FALLBACK_SWITCH = "security.fallback.switch"
AUDIT_START = "audit_start"
AUDIT_HEALTH = "audit_health"
AUDIT_RECOVERED = "audit_recovered"

_AUDIT_FILE_NAME = "security_audit.jsonl"

_BUFFER_CAPACITY = 1024
_BUFFER_WARN_COUNT = int(_BUFFER_CAPACITY * 0.8)
_RETRY_INTERVAL_S = 30.0
_FAIL_THRESHOLD = 5

# 模块级可靠性状态（_lock 保护；测试用 _reset_for_test 隔离）
_buffer: deque[dict[str, Any]] = deque(maxlen=_BUFFER_CAPACITY)
_dropped = 0                  # 缓冲溢出丢弃的最旧事件总数
_consecutive_failures = 0     # 连续写盘失败次数（含重试失败）
_degraded = False             # 是否已处于降级告警状态（避免重复告警）
_dirty = False                # 是否有过缓冲滞留（恢复后需补 audit_recovered）
_last_retry = 0.0             # 上次缓冲重试的 monotonic 时间
_lock = threading.Lock()


def _audit_file() -> Path:
    from jiuwenswarm.common.utils import get_logs_dir

    return get_logs_dir() / _AUDIT_FILE_NAME


def _write_event(event: dict[str, Any]) -> bool:
    """同步追加一行 JSONL；失败仅告警返回 False。"""
    try:
        path = _audit_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[security_lists] 审计落盘失败 kind=%s: %s", event.get("kind"), exc)
        return False


def _buffer_append_locked(event: dict[str, Any]) -> None:
    """入环形缓冲；满时丢最旧并计数（调用方须持锁）。"""
    global _dropped, _dirty
    if len(_buffer) >= _BUFFER_CAPACITY:
        _dropped += 1
    _buffer.append(event)
    _dirty = True


def _recover_locked() -> None:
    """写盘恢复后的收尾：补 audit_recovered、清降级态（调用方须持锁）。"""
    global _consecutive_failures, _degraded, _dropped, _dirty
    recovered = _dirty or _dropped > 0
    dropped, _dropped = _dropped, 0
    _dirty = False
    was_degraded = _degraded
    _consecutive_failures = 0
    _degraded = False
    if recovered:
        _write_event(
            {"ts": utc_now_iso(), "kind": AUDIT_RECOVERED, "dropped": dropped}
        )
    if was_degraded:
        logger.info("[security_lists] 审计落盘已恢复 dropped=%d", dropped)


def _enter_degraded_locked(reason: str) -> None:
    """进入降级态：落 audit_health 事件 + 锁外桌面提示（调用方须持锁）。"""
    global _degraded
    if _degraded:
        return
    _degraded = True
    health = {
        "ts": utc_now_iso(),
        "kind": AUDIT_HEALTH,
        "status": "degraded",
        "reason": reason,
        "consecutive_failures": _consecutive_failures,
        "buffered": len(_buffer),
        "dropped": _dropped,
    }
    if not _write_event(health):
        _buffer_append_locked(health)
    # 桌面提示放锁外会有时序竞争（状态已置），直接在锁内 best-effort 上报；
    # notify 内部已全 try/except，这里再兜一层确保绝不抛向业务路径
    try:
        from jiuwenswarm.agents.harness.common.rails.security_lists import notify

        notify.report_security_event_detached(
            stage="security.degraded",
            detail=f"安全审计落盘异常（{reason}），事件已缓冲，恢复后将自动补写",
        )
    except Exception:  # noqa: BLE001
        logger.warning("[security_lists] 审计降级告警上报失败", exc_info=True)


def _maybe_flush_locked() -> None:
    """事件驱动重试：缓冲非空且距上次重试 ≥30s 时尝试刷盘（调用方须持锁）。"""
    global _last_retry, _consecutive_failures
    if not _buffer:
        return
    now = time.monotonic()
    if now - _last_retry < _RETRY_INTERVAL_S:
        return
    _last_retry = now
    while _buffer:
        event = _buffer[0]
        if not _write_event(event):
            _consecutive_failures += 1
            if _consecutive_failures >= _FAIL_THRESHOLD:
                _enter_degraded_locked(f"连续 {_consecutive_failures} 次落盘失败")
            return
        _buffer.popleft()
    _recover_locked()


def _on_write_failure_locked(event: dict[str, Any]) -> None:
    """当前事件写盘失败：入缓冲 + 失败计数 + 健康检查（调用方须持锁）。"""
    global _consecutive_failures
    _buffer_append_locked(event)
    _consecutive_failures += 1
    if _consecutive_failures >= _FAIL_THRESHOLD:
        _enter_degraded_locked(f"连续 {_consecutive_failures} 次落盘失败")
    elif len(_buffer) >= _BUFFER_WARN_COUNT:
        _enter_degraded_locked(f"审计缓冲占用 {len(_buffer)}/{_BUFFER_CAPACITY}")


def _on_write_success_locked() -> None:
    """当前事件写盘成功（调用方须持锁）。

    缓冲非空：仅清零失败计数（盘已恢复），滞留事件待 30s 重试刷出后由
    flush 路径补 ``audit_recovered``；缓冲为空：直接按已恢复收尾。
    """
    global _consecutive_failures
    if _buffer:
        _consecutive_failures = 0
        return
    _recover_locked()


def log_event(kind: str, **fields: Any) -> bool:
    """追加一条审计事件（best-effort）：自动补 ``ts``/``kind``。

    写失败事件进内存环形缓冲等待重试，返回 False；不向调用方抛异常
    （审计不影响业务判定）。
    """
    event = {"ts": utc_now_iso(), "kind": str(kind)}
    event.update(fields)
    with _lock:
        _maybe_flush_locked()
        if _write_event(event):
            _on_write_success_locked()
            return True
        _on_write_failure_locked(event)
        return False


def self_check() -> bool:
    """启动自检：写 ``audit_start`` 验证可写；失败立即降级告警（不等首次事件）。"""
    global _consecutive_failures
    with _lock:
        if _write_event({"ts": utc_now_iso(), "kind": AUDIT_START}):
            _on_write_success_locked()
            return True
        _consecutive_failures += 1
        _buffer_append_locked({"ts": utc_now_iso(), "kind": AUDIT_START})
        _enter_degraded_locked("启动自检写盘失败")
        return False


def _reset_for_test() -> None:
    """清空全部可靠性状态（测试隔离用）。"""
    global _consecutive_failures, _degraded, _dropped, _dirty, _last_retry
    with _lock:
        _buffer.clear()
        _dropped = 0
        _consecutive_failures = 0
        _degraded = False
        _dirty = False
        _last_retry = 0.0


def query_events(
    *,
    kind: str | None = None,
    since: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """按时间正序返回最近 ``limit`` 条事件（RPC audit.query 用）。

    ``since`` 为 ISO8601 字符串下界（含）；损坏行跳过（与引擎宽容语义一致）。
    """
    path = _audit_file()
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        logger.warning("[security_lists] 审计读取失败: %s", exc)
        return []

    events: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if kind is not None and event.get("kind") != kind:
            continue
        if since is not None and str(event.get("ts") or "") < since:
            continue
        events.append(event)
    limit = max(1, min(int(limit or 200), 2000))
    return events[-limit:]


__all__ = [
    "AUDIT_CHANGE",
    "AUDIT_FALLBACK",
    "AUDIT_FALLBACK_SWITCH",
    "AUDIT_HEALTH",
    "AUDIT_HIT",
    "AUDIT_RECOVERED",
    "AUDIT_START",
    "log_event",
    "query_events",
    "self_check",
]
