# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cron-specific final-reply fallbacks."""

from __future__ import annotations

from typing import Any


CRON_EMPTY_REPLY_FALLBACK = "[cron] 任务执行完成但未返回结果内容"


def cron_empty_reply_fallback(params: Any) -> str | None:
    """Return a persisted-visible fallback for a cron run without final text."""
    if not isinstance(params, dict):
        return None
    if not isinstance(params.get("cron"), dict):
        return None
    return CRON_EMPTY_REPLY_FALLBACK
