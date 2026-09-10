# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""跨模块基础设施工具（时间、日志脱敏指纹等）。"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

# 统一敏感信息掩码值（日志引擎 / debug_trace 结构化脱敏共用）。
SENSITIVE_MASK = "******"
_ALREADY_MASKED_PATTERN = re.compile(
    rf"^{re.escape(SENSITIVE_MASK)}(\(fp:[0-9a-f]{{8}}\))?$"
)


def utc_now() -> datetime:
    """返回当前 UTC 时间（带 ``timezone.utc`` 的 aware ``datetime``）。"""
    return datetime.now(timezone.utc)


def fingerprint(value: str) -> str:
    """返回 value 的 SHA256 前 4 字节（8 位 hex）指纹。

    不可逆：拿到 ``fp:7f3a2c19`` 无法还原原值。同一 key 每次指纹一致，
    可在日志中把同一账号/会话的多次请求串起来排查；key 轮换后指纹自然变化。
    """
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:8]


def is_already_masked(value: Any) -> bool:
    """判断 value 是否已是脱敏产物（纯掩码或带指纹），避免重复脱敏。"""
    try:
        v = str(value) if value is not None else ""
    except Exception:
        return False
    return bool(v) and bool(_ALREADY_MASKED_PATTERN.match(v))


def masked_with_fp(value: Any) -> str:
    """凭证类脱敏：``******(fp:xxxxxxxx)``，便于跨日志关联同一 key。

    若 value 本身已是脱敏产物，原样返回，不重算指纹。
    是否调用本函数由规则字段 ``with_fingerprint`` 决定。
    """
    try:
        v = str(value) if value is not None else ""
    except Exception:
        return SENSITIVE_MASK
    if is_already_masked(v):
        return v
    fp = fingerprint(v)
    if not fp:
        return SENSITIVE_MASK
    return f"{SENSITIVE_MASK}(fp:{fp})"
