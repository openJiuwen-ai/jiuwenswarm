# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Transport-level Web request identity helpers.

分层约定（企业版与个人版共用同一结构，按字段必填性区分）：

1. **Wire**（浏览器 → Gateway，非权威）
   - WS query：``user_id`` / ``group_id`` / ``bot_id`` / ``gateway_id``
   - HTTP 头：``X-User-Id`` / ``X-Group-Id`` / ``X-Bot-Id`` / ``X-Gateway-Id``

2. **进站后权威落点**

   - ``metadata["user_id"]``（顶层）—— 谁在说话；并镜像到
     ``Message.user_id`` / ``E2AEnvelope.user_id``
   - ``metadata["routing"] = {group_id, bot_id, gateway_id}``—— 新增路由维
     （E2A 上对应 ``channel_context.routing``）

   Gateway 入口用 :func:`normalize_routing_identity` + :func:`apply_routing_metadata`
   写一次；下游用 :func:`web_routing_identity` 一次读全（含顶层 ``user_id``）。

3. **不要**：把身份写入业务 ``params``；不要把 ``group_id``/``bot_id``/``gateway_id``
   摊到 metadata 顶层；不要把 ``user_id`` 放进 ``routing``。

4. **Gateway → Agent**
   - REST（body 仅 params）：经 ``X-*`` 头透传，Agent 重建顶层 ``user_id`` + ``routing``；
     企业租户另透传 ``X-Service-Id`` / ``X-Agent-Id`` / ``X-Workspace-Key``
   - 整封 E2A：带 ``user_id`` 与 ``channel_context.routing``，以及顶层租户三字段

5. **必填策略**
   - 个人版 Web：建议有顶层 ``user_id``；其余可缺
   - 企业版 Web：``routing.bot_id`` 必有；``user_id``/``group_id`` 按策略需要；
     ``gateway_id`` 有则传（Agent 业务暂不强制）

6. **本地例外**：Gateway 本地 handler（如 ``cron.*``）可用
   :func:`merge_routing_into_params` 做**调用副本**，不回写 Message。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ROUTING_METADATA_KEY = "routing"
USER_ID_FIELD = "user_id"
# 仅这些写入 metadata.routing；user_id 在 metadata 顶层。
WEB_ROUTING_ID_FIELDS = ("group_id", "bot_id", "gateway_id")
WEB_IDENTITY_FIELDS = (USER_ID_FIELD, *WEB_ROUTING_ID_FIELDS)


def _identity_value(value: Any) -> str | None:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _pick_field(mapping: Mapping[str, Any], field: str) -> str | None:
    return _identity_value(mapping.get(field))


def normalize_routing_identity(
    *sources: Mapping[str, Any] | None,
) -> dict[str, str]:
    """从若干 mapping 归一化完整身份（含 ``user_id``）；同一字段以先出现的非空值为准。

    每个 source 可以是扁平 dict、``parse_qs`` 风格，或已含部分字段的 identity/routing dict。
    写入时请交给 :func:`apply_routing_metadata` 拆到顶层 / ``routing``。
    """
    identity: dict[str, str] = {}
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for field in WEB_IDENTITY_FIELDS:
            if field in identity:
                continue
            value = _pick_field(source, field)
            if value:
                identity[field] = value
    return identity


def apply_routing_metadata(
    metadata: Mapping[str, Any] | None,
    routing: Mapping[str, str] | None,
) -> dict[str, Any]:
    """写入身份：``user_id`` → 顶层；``group_id``/``bot_id``/``gateway_id`` → ``routing``。

    同时清掉顶层的 routing 三字段，以及 ``routing`` 内遗留的 ``user_id``。
    """
    meta = dict(metadata or {})
    identity = dict(routing or {})

    for field in WEB_ROUTING_ID_FIELDS:
        meta.pop(field, None)

    if USER_ID_FIELD in identity:
        user_id = _identity_value(identity.get(USER_ID_FIELD))
        if user_id:
            meta[USER_ID_FIELD] = user_id
        else:
            meta.pop(USER_ID_FIELD, None)

    cleaned = {
        field: value
        for field, value in identity.items()
        if field in WEB_ROUTING_ID_FIELDS and str(value or "").strip()
    }
    if cleaned:
        meta[ROUTING_METADATA_KEY] = cleaned
    else:
        meta.pop(ROUTING_METADATA_KEY, None)
    return meta


def web_routing_identity(metadata: Mapping[str, Any] | None) -> dict[str, str]:
    """读完整身份：顶层 ``user_id`` + ``metadata.routing`` 三字段。"""
    if not isinstance(metadata, Mapping):
        return {}
    identity: dict[str, str] = {}
    user_id = _pick_field(metadata, USER_ID_FIELD)
    if user_id:
        identity[USER_ID_FIELD] = user_id
    routing = metadata.get(ROUTING_METADATA_KEY)
    if isinstance(routing, Mapping):
        for field in WEB_ROUTING_ID_FIELDS:
            value = _pick_field(routing, field)
            if value:
                identity[field] = value
    return identity


def merge_routing_into_params(
    params: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
    *,
    override: bool = True,
) -> dict[str, Any]:
    """仅用于 Gateway **本地** handler（如 cron.*）：handler 签名只有 params。

    AgentRequest.params 不应调用此函数；Agent 侧请读顶层 ``user_id`` /
    ``metadata.routing``（或 :func:`web_routing_identity`）。
    """
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
