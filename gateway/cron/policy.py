# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""定时任务个数上限的数值规整。

企业管理面写入 ``cron_policy`` 时复用本模块的 ``coerce_max_jobs_per_user``。
"""

from __future__ import annotations

from typing import Any

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
