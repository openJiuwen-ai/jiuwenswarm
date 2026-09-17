# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Auto-register the proactive-recommendation tick cron job.

``ProactiveEngine`` does not run its own loop — it relies on an external caller
to invoke ``tick_now()``.  The scheduler already knows how to handle a job
with ``mode == "proactive.tick"`` (it sends a ``PROACTIVE_TICK`` WS request to
the AgentServer, which calls ``tick_now()``), but nothing was creating such a
job.  This module reconciles the cron store with the
``proactive_recommendation`` config:

* ``enabled == true``  → ensure a ``proactive.tick`` job exists (created if
  missing with a default schedule; left untouched if already present, so the
  schedule edited in the Cron panel is preserved).
* ``enabled == false`` → delete the job if it exists.

The schedule (cron expression / offset / timezone / target channel) is owned
by the Cron panel once the job exists — this module only seeds the initial
defaults on first creation and never overwrites them afterwards.

Called from two places:
  1. Gateway startup, after the cron scheduler is started.
  2. ``_on_config_saved``, when the proactive ``enabled`` key changes.

Idempotent: the job uses a fixed id (``PROACTIVE_JOB_ID``) so repeated calls
never produce duplicates.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

PROACTIVE_JOB_ID = "proactive-tick-auto"
PROACTIVE_JOB_NAME = "主动推荐定时检查"
# 首次创建时使用的默认调度：每 1 小时一次（每小时第 0 分 20 秒），秒字段 20 错峰。
# 仅用于 seed；job 创建后由 Cron 面板接管，此处不再覆盖。
DEFAULT_CRON_EXPR = "20 0 * * * * *"
# 推荐推送到哪个通道。scheduler 消费 proactive.tick 时把 job.targets 当作
# target_channel 传给 ProactiveEngine.tick_now()。web 是最常见的展示通道；
# 用户若主要用 TUI/IM，可在 cron 面板手动改为对应 channel。
DEFAULT_TARGET_CHANNEL = "web"


def _get_proactive_cfg(config_payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config_payload, dict):
        return {}
    cfg = config_payload.get("proactive_recommendation")
    return cfg if isinstance(cfg, dict) else {}


async def sync_proactive_tick_job(
    cron_controller: Any,
    config_payload: dict[str, Any] | None,
) -> None:
    """根据 config 同步 proactive.tick job（创建/删除）。幂等。

    只看 ``enabled``：开则确保 job 存在（缺失才创建，已存在不动以保留面板编辑），
    关则删除。调度表达式不再由 config 驱动。
    """
    if cron_controller is None:
        logger.debug("[ProactiveAutoReg] cron_controller is None, skip")
        return

    try:
        cfg = _get_proactive_cfg(config_payload)
        enabled = bool(cfg.get("enabled", False))

        if not enabled:
            await _ensure_deleted(cron_controller)
            return

        await _ensure_present(cron_controller)
    except Exception as exc:  # noqa: BLE001
        # 不影响 config 保存主流程
        logger.warning("[ProactiveAutoReg] sync failed (non-fatal): %s", exc, exc_info=True)


async def _ensure_present(cron_controller: Any) -> None:
    """确保 proactive.tick job 存在。

    缺失则用默认调度创建；已存在则完全不动 —— 调度表达式 / 偏移 / 时区 /
    推送频道均由 Cron 面板接管，避免覆盖用户的编辑。
    """
    existing = await _find_job(cron_controller)
    if existing is None:
        params = {
            "id": PROACTIVE_JOB_ID,
            "name": PROACTIVE_JOB_NAME,
            "cron_expr": DEFAULT_CRON_EXPR,
            "timezone": "Asia/Shanghai",
            "enabled": True,
            "description": "由主动推荐开关自动创建/删除；调度表达式可在定时任务面板编辑",
            "targets": DEFAULT_TARGET_CHANNEL,
            "mode": "proactive.tick",
            # wake_offset=0：proactive.tick 到点就执行，不提前 wake。
            # 否则 wake_dt 在过去会导致循环触发。
            "wake_offset_seconds": 0,
        }
        await cron_controller.create_job(params)
        logger.info("[ProactiveAutoReg] created proactive.tick job (cron=%s)", DEFAULT_CRON_EXPR)
        return

    logger.debug("[ProactiveAutoReg] job already exists, leaving schedule untouched (cron=%s)",
                 (existing.get("cron_expr") or "").strip())


async def _ensure_deleted(cron_controller: Any) -> None:
    existing = await _find_job(cron_controller)
    if existing is None:
        return
    # force=True：这是 config 开关关闭触发的合法删除，绕过 store 层对
    # proactive.tick job 的手动删除保护（保护只挡用户路径，不挡 sync）。
    await cron_controller.delete_job(PROACTIVE_JOB_ID, force=True)
    logger.info("[ProactiveAutoReg] deleted proactive.tick job (disabled in config)")


async def _find_job(cron_controller: Any) -> dict[str, Any] | None:
    """Return the auto-managed proactive job dict if it exists in the store."""
    try:
        job = await cron_controller.get_job(PROACTIVE_JOB_ID)
    except Exception:
        job = None
    if job is not None:
        return job
    # get_job may return None for some controllers; fall back to listing.
    try:
        for j in await cron_controller.list_jobs():
            if isinstance(j, dict) and j.get("id") == PROACTIVE_JOB_ID:
                return j
    except Exception as exc:
        logger.debug("[ProactiveAutoReg] list_jobs fallback failed: %s", exc)
    return None
