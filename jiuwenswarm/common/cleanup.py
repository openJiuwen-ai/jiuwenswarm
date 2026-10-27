# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Periodic background cleanup for old session data and file_ops logs.

Mirrors claude-code's ``cleanup.ts`` approach: periodically check directory mtime
and remove entries older than the configured retention period.

安全约束（清理是本仓最 destructive 的后台动作，宁可少删不可误删）：

- 保留期取自 ``CLEANUP_PERIOD_DAYS``，**非法值（非整数 / ``0`` / 负数）一律回落默认
  30 天并告警**；缺失或空串属"未配置"，静默回落。``0`` / 负数若直接参与
  ``cutoff = now - days*86400``，cutoff 会落在现在或未来，使所有会话目录都判定为过期
  而被全量删除（不可恢复的数据丢失）。非法值回落的约定与本包
  ``common/workspace/quota.py`` 一致。
- 超大值（> ``MAX_CLEANUP_PERIOD_DAYS``）夹紧到上界并告警。上界的存在同时挡掉
  ``days*86400`` 的 float 溢出：极大值会让 cutoff 计算抛 OverflowError，被
  ``run_cleanup`` 吞掉后整轮会话清理静默不执行（孤儿 file_ops 清理照跑）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Callable

from jiuwenswarm.common.utils import (
    get_agent_workspace_dir,
    get_agent_sessions_dir,
    get_user_workspace_dir,
)

logger = logging.getLogger(__name__)

DEFAULT_CLEANUP_PERIOD_DAYS = 30
# 保留期上界，取 100 年——再长没有业务意义。
# 上界本身是硬要求：days*86400 超出 float 可表示范围（约 2e304 天）后
# `time.time() - days*86400` 会抛 OverflowError，让整轮会话清理被 run_cleanup 吞掉。
# 命中上界时取"夹紧"而非"回落默认值"：极大值语义是"什么都别删"，夹紧能保住该意图，
# 回落 30 天反而会突然开始删掉运维本来想保留的数据。
MAX_CLEANUP_PERIOD_DAYS = 36500
RECURRING_CLEANUP_INTERVAL_S = 24 * 60 * 60
FIRST_CLEANUP_DELAY_S = 10 * 60
AGENT_ID = "jiuwenswarm"

CLEANUP_PERIOD_ENV = "CLEANUP_PERIOD_DAYS"

_LOG_VALUE_MAX_CHARS = 32


def _short(raw: str) -> str:
    """截断超长环境变量值，避免畸形配置（如 400 位数字）刷屏日志。

    只负责截断，**渲染时仍必须配 ``%r``**：repr 负责转义换行等控制字符，
    否则 ``CLEANUP_PERIOD_DAYS="0\\nFAKE-LOG-LINE"`` 这类值会把日志拆行、
    伪造出一条无前缀的假日志（``common/workspace/quota.py`` 也是 ``%r``）。
    """
    if len(raw) <= _LOG_VALUE_MAX_CHARS:
        return raw
    return raw[:_LOG_VALUE_MAX_CHARS] + "..."


def _parse_period_days(raw: str) -> int | None:
    """把配置值解析成天数；不是整数时返回 ``None``。

    CPython ≥3.11 给 int-str 转换设了位数上限（默认 4300），超长数字串会抛
    ``ValueError``。这类值语义明确是"极大"，返回 ``MAX_CLEANUP_PERIOD_DAYS + 1``
    让它走夹紧分支——当成"不是数字"回落默认值会恰好走到本函数想避免的那条路
    （30 天，开始真删数据）。用 ``isdecimal()`` 而不是 ``isdigit()``：后者对
    ``'²'`` 这类字符也为真，但 ``int()`` 解析不了，会误判成"数字过大"。
    """
    try:
        return int(raw)
    except (TypeError, ValueError):
        return MAX_CLEANUP_PERIOD_DAYS + 1 if raw.isdecimal() else None


def _get_cleanup_period_days() -> int:
    """保留期天数；非整数 / 非正值回落默认值并告警，超大值夹紧到上界。

    缺失或空串属"未配置"，静默回落（不告警）；``days <= 0`` 会让
    ``_get_cutoff_timestamp()`` 的 cutoff 落在现在或未来，从而删除**全部**会话目录，
    因此必须挡在入口而不是靠调用方自觉。
    """
    raw = os.getenv(CLEANUP_PERIOD_ENV, "").strip()
    if not raw:
        return DEFAULT_CLEANUP_PERIOD_DAYS
    days = _parse_period_days(raw)
    if days is None:
        logger.warning(
            "background_cleanup: invalid %s=%r; using default %dd",
            CLEANUP_PERIOD_ENV,
            _short(raw),
            DEFAULT_CLEANUP_PERIOD_DAYS,
        )
        return DEFAULT_CLEANUP_PERIOD_DAYS
    if days <= 0:
        logger.warning(
            "background_cleanup: non-positive %s=%r; using default %dd "
            "(0 / negative is invalid)",
            CLEANUP_PERIOD_ENV,
            _short(raw),
            DEFAULT_CLEANUP_PERIOD_DAYS,
        )
        return DEFAULT_CLEANUP_PERIOD_DAYS
    if days > MAX_CLEANUP_PERIOD_DAYS:
        logger.warning(
            "background_cleanup: %s=%r exceeds max; clamped to %dd",
            CLEANUP_PERIOD_ENV,
            _short(raw),
            MAX_CLEANUP_PERIOD_DAYS,
        )
        return MAX_CLEANUP_PERIOD_DAYS
    return days


def _get_cutoff_timestamp(days: int) -> float:
    """保留期对应的过期时间戳；早于该 mtime 的目录视为过期。

    ``days`` 必须是 ``_get_cleanup_period_days()`` 夹紧后的值——超大整数在这里
    会因 ``time.time() - days * 86400`` 的 float 转换抛 OverflowError。
    """
    return time.time() - days * 24 * 60 * 60


def _rmtree(path: Path) -> bool:
    """删除目录树，返回路径是否**确认**已不存在。

    ``rmtree`` 用 ``ignore_errors=True``（保证幂等、不因单个文件失败中断整轮），
    代价是失败被完全吞掉，所以这里用 ``os.lstat`` 回查一次：只有 ``FileNotFoundError``
    才算删干净；仍存在、或无法判定（父目录不可访问等）一律返回 False 交给调用方计
    error。不用 ``Path.exists()`` / ``os.path.lexists``——两者都把 OSError 吞成
    False，会把"没删掉 / 查不了"误报成删除成功。lstat 不跟随符号链接，故残留的
    软链目录也会如实判为"仍存在"。

    告警前缀沿用调用方 ``cleanup_old_sessions:``（本模块日志前缀按所属流程统一，
    不用匿名的 ``cleanup:``），便于按函数名过滤。
    """
    import shutil

    shutil.rmtree(path, ignore_errors=True)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        logger.warning(
            "cleanup_old_sessions: cannot verify removal of %s", path, exc_info=True
        )
        return False
    return False


def cleanup_old_sessions() -> dict[str, int]:
    """清理超过保留期的会话目录（按目录 mtime 判断，与 cc 一致）。

    Returns:
        { "removed": N, "errors": N }
    """
    retention_days = _get_cleanup_period_days()
    cutoff = _get_cutoff_timestamp(retention_days)
    sessions_dir = get_agent_sessions_dir()
    if not sessions_dir.is_dir():
        return {"removed": 0, "errors": 0}

    removed = 0
    errors = 0

    for entry in sessions_dir.iterdir():
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
        except OSError:
            errors += 1
            continue
        if _rmtree(entry):
            removed += 1
            logger.info("cleanup_old_sessions: removed %s", entry.name)
        else:
            errors += 1
            logger.warning("cleanup_old_sessions: failed to remove %s", entry)

    if removed or errors:
        logger.info(
            "cleanup_old_sessions: removed=%d errors=%d cutoff_ts=%.0f retention=%dd",
            removed, errors, cutoff, retention_days,
        )
    return {"removed": removed, "errors": errors}


def cleanup_orphan_file_ops() -> dict[str, int]:
    """清理对应会话目录已不存在的 file_ops 日志。

    Returns:
        { "removed": N, "errors": N }
    """
    sessions_dir = get_agent_sessions_dir()
    existing_sessions: set[str] = set()
    if sessions_dir.is_dir():
        for entry in sessions_dir.iterdir():
            if entry.is_dir():
                existing_sessions.add(entry.name)

    removed = 0
    errors = 0

    for base_dir in (get_agent_workspace_dir(), get_user_workspace_dir()):
        hist_dir = base_dir / ".agent_history"
        if not hist_dir.is_dir():
            continue
        for f in hist_dir.iterdir():
            if not f.is_file():
                continue
            name = f.name
            # flash edit_file 的追加账本：file_ops_flash_{AGENT_ID}_* 前缀 +
            # .jsonl 后缀（注意不能只查 file_ops_{AGENT_ID}_ 前缀——flash
            # 账本多一段 flash_，前缀不匹配会导致孤儿 .jsonl 永不清理）。
            if not name.startswith(
                (f"file_ops_{AGENT_ID}_", f"file_ops_flash_{AGENT_ID}_")
            ) or not name.endswith((".json", ".jsonl")):
                continue
            # 检查文件名中是否包含某个现存 session 的 id
            orphaned = True
            for sid in existing_sessions:
                if sid in name:
                    orphaned = False
                    break
            if not orphaned:
                continue
            try:
                f.unlink()
                removed += 1
                logger.info("cleanup_orphan_file_ops: removed %s", f.name)
            except OSError:
                errors += 1

    if removed or errors:
        logger.info(
            "cleanup_orphan_file_ops: removed=%d errors=%d",
            removed, errors,
        )
    return {"removed": removed, "errors": errors}


async def run_cleanup() -> None:
    """执行一次完整的清理周期。"""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, cleanup_old_sessions)
    except Exception:
        logger.warning("background_cleanup: cleanup_old_sessions failed", exc_info=True)

    try:
        await loop.run_in_executor(None, cleanup_orphan_file_ops)
    except Exception:
        logger.warning("background_cleanup: cleanup_orphan_file_ops failed", exc_info=True)


async def cleanup_loop(
    stop_event: asyncio.Event,
    on_first_done: Callable[[], None] | None = None,
) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=FIRST_CLEANUP_DELAY_S)
        return
    except asyncio.TimeoutError:
        pass

    first = True
    while not stop_event.is_set():
        logger.info("background_cleanup: starting cleanup cycle")
        await run_cleanup()
        if first and on_first_done is not None:
            on_first_done()
            first = False
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=RECURRING_CLEANUP_INTERVAL_S)
            return
        except asyncio.TimeoutError:
            pass


def start_background_cleanup(
    on_first_done: Callable[[], None] | None = None,
) -> asyncio.Task:
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        cleanup_loop(stop_event, on_first_done=on_first_done),
        name="background-cleanup",
    )
    task.add_done_callback(lambda _: stop_event.set())
    logger.info(
        "background_cleanup: started (first_delay=%ds, interval=%ds, retention=%dd)",
        FIRST_CLEANUP_DELAY_S,
        RECURRING_CLEANUP_INTERVAL_S,
        _get_cleanup_period_days(),
    )
    return task
