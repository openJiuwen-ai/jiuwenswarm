# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Front/Control-safe E2A error and param helpers. No Runtime adapter imports."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse


def parse_int_param(
    params: dict[str, Any] | None,
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """宽松解析整型参数（与 Web fallback 一致：int/整型 float/数字字符串）。

    非法值回落到 ``default``，随后夹取到 ``[minimum, maximum]``。
    """
    value = default
    raw = (params or {}).get(key)
    if isinstance(raw, int) and not isinstance(raw, bool):
        value = raw
    elif isinstance(raw, float) and raw.is_integer():
        value = int(raw)
    elif isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
    value = max(minimum, min(value, maximum))
    return value


def build_error_response(
    request: AgentRequest,
    error: str,
    code: str = "INTERNAL_ERROR",
    *,
    ok: bool = False,
    extra: dict[str, Any] | None = None,
) -> AgentResponse:
    """异常/失败映射为统一的失败 AgentResponse。"""
    payload: dict[str, Any] = {"error": str(error), "code": code}
    if extra:
        payload.update(extra)
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=ok,
        payload=payload,
        metadata=request.metadata,
    )
