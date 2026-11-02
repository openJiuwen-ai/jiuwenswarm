# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""界面定时任务个数上限：YAML 默认，企业库 cron_policy 覆盖。

只供 Web 面板读取（cron.job.meta）。创建接口本身不拒绝，会话和 TUI 不受这项约束。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

CRON_POLICY_TABLE = "cron_policy"
DEFAULT_MAX_JOBS_PER_USER = 5


def coerce_max_jobs_per_user(
    raw: Any,
    *,
    default: int = DEFAULT_MAX_JOBS_PER_USER,
) -> int:
    """把配置值收成非负整数。缺失或非法时用默认值；0 表示界面不能再启用新任务。"""
    if raw is None or isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < 0:
        return default
    return value


def max_jobs_per_user_from_config() -> int:
    """读取 config.yaml 的 ``cron.max_jobs_per_user``。"""
    try:
        from jiuwenswarm.common.config import get_config

        cfg = get_config() or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[CronPolicy] yaml read failed: %s", exc)
        return DEFAULT_MAX_JOBS_PER_USER
    section = cfg.get("cron") if isinstance(cfg, dict) else None
    if not isinstance(section, dict):
        return DEFAULT_MAX_JOBS_PER_USER
    if "max_jobs_per_user" not in section:
        return DEFAULT_MAX_JOBS_PER_USER
    return coerce_max_jobs_per_user(section.get("max_jobs_per_user"))


async def resolve_max_jobs_per_user() -> int:
    """企业库有 cron_policy 行时用该行，否则用 YAML。"""
    try:
        from jiuwenswarm.edition import is_enterprise

        if not is_enterprise():
            return max_jobs_per_user_from_config()
    except Exception:  # noqa: BLE001
        return max_jobs_per_user_from_config()

    try:
        from jiuwenswarm.gateway.config.enterprise.access import (
            get_enterprise_record_repository,
        )

        repo = get_enterprise_record_repository(CRON_POLICY_TABLE)
        if repo is None:
            return max_jobs_per_user_from_config()
        row = await repo.get()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[CronPolicy] db read failed, using yaml: %s", exc)
        return max_jobs_per_user_from_config()

    if not isinstance(row, dict) or row.get("max_jobs_per_user") is None:
        return max_jobs_per_user_from_config()
    return coerce_max_jobs_per_user(row.get("max_jobs_per_user"))
