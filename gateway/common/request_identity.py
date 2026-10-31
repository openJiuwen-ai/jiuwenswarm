# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Transport-level Web request identity helpers.

分层约定（企业版与个人版共用同一结构，按字段必填性区分）：

1. **Wire**（浏览器 → Gateway，非权威）
   - WS query：``user_id`` / ``group_id`` / ``bot_id`` / ``gateway_id``
   - HTTP 头：``X-User-Id`` / ``X-Group-Id`` / ``X-Bot-Id`` / ``X-Gateway-Id``

2. **进站后权威落点**

   - ``metadata["user_id"]``（顶层）—— 谁在说话；并镜像到
     ``Message.user_id`` / ``E2AEnvelope.user_id``
   - ``metadata["routing"] = {group_id, bot_id, gateway_id}``—— 路由维
     （E2A 上对应 ``channel_context.routing``）

   Gateway 入口用 :func:`normalize_routing_identity` + :func:`apply_routing_metadata`
   写一次；下游用 :func:`web_routing_identity` 一次读全（含顶层 ``user_id``）。

3. **不要**：把身份写入业务 ``params``；不要把 ``group_id``/``bot_id``/``gateway_id``
   摊到 metadata 顶层；不要把 ``user_id`` 放进 ``routing``。

4. **必填策略**
   - 个人版 Web：建议有顶层 ``user_id``；其余可缺
   - 企业版 Web：``routing.bot_id`` 建议有；``user_id``/``group_id`` 按策略需要

实现已收口到 ``openjiuwen_protocol.identity``（gateway 与 AgentServer 共用的
唯一来源）；本模块只 re-export 并保留 gateway 本地 handler 的
``merge_routing_into_params`` 适配。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from openjiuwen_protocol.identity import (
    ROUTING_METADATA_KEY,
    USER_ID_FIELD,
    WEB_IDENTITY_FIELDS,
    WEB_ROUTING_ID_FIELDS,
    apply_routing_metadata,
    normalize_routing_identity,
    web_routing_identity,
)


def merge_routing_into_params(
    params: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
    *,
    override: bool = True,
) -> dict[str, Any]:
    """仅用于 Gateway **本地** handler：handler 签名只有 params。"""
    result = dict(params or {})
    for field, value in web_routing_identity(metadata).items():
        if override or field not in result:
            result[field] = value
    return result


__all__ = (
    "ROUTING_METADATA_KEY",
    "USER_ID_FIELD",
    "WEB_ROUTING_ID_FIELDS",
    "WEB_IDENTITY_FIELDS",
    "normalize_routing_identity",
    "apply_routing_metadata",
    "merge_routing_into_params",
    "web_routing_identity",
)
