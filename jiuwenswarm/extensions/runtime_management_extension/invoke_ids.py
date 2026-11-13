# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业版默认 service_id / agent_id / workspace_key 拼接（与旧 RuntimeManagement 路由策略一致）。"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _default_invoke_ids(group_id: str, bot_id: str, user_id: str) -> tuple[str, str]:
    """企业策略未配置 service_id/agent_id 时的默认拼接。"""
    default_service_id = f"{group_id}{bot_id}"
    default_agent_id = f"{group_id}{bot_id}{user_id}"
    logger.info(
        "user_id=%s, group_id=%s, bot_id=%s, default_service_id=%s, default_agent_id=%s",
        user_id,
        group_id,
        bot_id,
        default_service_id,
        default_agent_id,
    )
    return default_service_id, default_agent_id


def _default_workspace_key(group_id: str, bot_id: str, user_id: str) -> str:
    """未配置 workspace_key 时默认 ``{group}{bot}{user}``。"""
    return f"{group_id}{bot_id}{user_id}".strip()


def _md5_invoke_id(value: str) -> str:
    """逻辑 ID → MD5 32 位 hex。"""
    text = str(value or "").strip()
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def coalesce_invoke_ids(
    *,
    group_id: str = "",
    bot_id: str = "",
    user_id: str = "",
    service_id: str | None = None,
    agent_id: str | None = None,
    workspace_key: str | None = None,
) -> tuple[str, str, str]:
    """已有非空 id 原样保留；缺失时用路由三元组拼默认逻辑字符串（尚未 MD5）。

    ``agent_id`` 只认信封顶层，不再从 ``agent_ref`` 推导，故此处只需空串判断。
    入参格式由调用方保证，本函数不做 strip / 类型规整。
    """
    if group_id and bot_id and user_id:
        if not service_id or not agent_id:
            default_service_id, default_agent_id = _default_invoke_ids(
                group_id, bot_id, user_id
            )
            if not service_id:
                service_id = default_service_id
            if not agent_id:
                agent_id = default_agent_id
        if not workspace_key:
            workspace_key = _default_workspace_key(group_id, bot_id, user_id)

    return (
        service_id or "default_service_id",
        agent_id or "default_agent_id",
        workspace_key or "default_workspace_key",
    )


def routing_triple_from_envelope(envelope: Any) -> tuple[str, str, str]:
    """只从权威源取 ``(group_id, bot_id, user_id)``。

    权威：``channel_context`` 上的顶层 ``user_id`` + ``routing``（见
    :func:`web_routing_identity`）；``envelope.user_id`` 为同字段协议镜像。
    """
    from jiuwenswarm.common.request_identity import web_routing_identity

    ctx = (
        envelope.channel_context
        if isinstance(getattr(envelope, "channel_context", None), dict)
        else None
    )
    identity = web_routing_identity(ctx)
    group_id = str(identity.get("group_id") or "").strip()
    bot_id = str(identity.get("bot_id") or "").strip()
    user_id = str(
        identity.get("user_id") or getattr(envelope, "user_id", None) or ""
    ).strip()
    return group_id, bot_id, user_id


def apply_invoke_ids_to_envelope(envelope: Any) -> Any:
    """补齐并 MD5 化信封顶层 ``service_id`` / ``agent_id`` / ``workspace_key``。

    非空顶层字段优先；空串视为未配置，改用权威 routing 三元组拼默认值。
    """
    group_id, bot_id, user_id = routing_triple_from_envelope(envelope)

    service_id, agent_id, workspace_key = coalesce_invoke_ids(
        group_id=group_id,
        bot_id=bot_id,
        user_id=user_id,
        service_id=getattr(envelope, "service_id", None),
        agent_id=getattr(envelope, "agent_id", None),
        workspace_key=getattr(envelope, "workspace_key", None),
    )
    hashed_service_id = _md5_invoke_id(service_id)
    hashed_agent_id = _md5_invoke_id(agent_id)
    hashed_workspace_key = _md5_invoke_id(workspace_key)

    logger.info(
        "[invoke_ids] envelope tenant ids: "
        "group=%s bot=%s user=%s logical=(%s,%s,%s) hashed=(%s,%s,%s)",
        group_id,
        bot_id,
        user_id,
        service_id,
        agent_id,
        workspace_key,
        hashed_service_id,
        hashed_agent_id,
        hashed_workspace_key,
    )

    envelope.service_id = hashed_service_id
    envelope.agent_id = hashed_agent_id
    envelope.workspace_key = hashed_workspace_key
    return envelope
