# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""OpenAbility WebSocket 线格式。

出站（Gateway → OA）：``sandboxId`` + ``msgDetail``（内层 E2A JSON 字符串），建连时带 ``x-hag-trace-id``。
入站（OA → Gateway）：裸内层 E2A JSON 字符串（原 ``msgDetail`` 字段值），需 ``json.loads`` 为 dict。
"""

from __future__ import annotations

import json
from typing import Any

OPEN_ABILITY_TRACE_HEADER = "x-hag-trace-id"
OPEN_ABILITY_GATEWAY_TRACE_ID = "jiuwen-gateway"


def open_ability_connect_headers() -> dict[str, str]:
    """Gateway 连接 OpenAbility 时使用的 WebSocket 握手请求头。"""
    return {OPEN_ABILITY_TRACE_HEADER: OPEN_ABILITY_GATEWAY_TRACE_ID}


def wrap_openability_message(sandbox_id: str, detail: dict[str, Any]) -> dict[str, Any]:
    """将内层 E2A 线 dict 封装为 OpenAbility 外层帧。"""
    sid = str(sandbox_id or "").strip()
    if not sid:
        raise ValueError("wrap_openability_message: sandbox_id is required")
    return {
        "sandboxId": sid,
        "msgDetail": json.dumps(detail, ensure_ascii=False),
    }


def parse_openability_inbound_message(data: Any) -> dict[str, Any]:
    """解析 OpenAbility 入站帧：裸 E2A JSON 字符串 → dict（原 msgDetail 内容）。"""
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    if isinstance(data, str):
        text = data.strip()
        if not text:
            raise ValueError("parse_openability_inbound_message: frame is empty")
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError(
                "parse_openability_inbound_message: JSON must decode to an object"
            )
        return parsed
    if isinstance(data, dict):
        return dict(data)
    raise ValueError(
        f"parse_openability_inbound_message: frame must be str or dict, "
        f"got {type(data).__name__}"
    )
