# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request → tenant_key helpers that stay Front/control-plane safe.

Must not import ``AgentManager`` / harness; Front control queries load this
module while Runtime is still warming.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

from jiuwenswarm.enterprise.tenant.tenant_paths import (
    build_tenant_key,
    normalize_tenant_key_part,
)
from jiuwenswarm.enterprise import is_enterprise
from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext

logger = logging.getLogger(__name__)


def normalize_part(value: str | None, *, default: str = "default") -> str:
    return normalize_tenant_key_part(value, default=default)


def extract_triple(request: Any) -> tuple[str, str, str]:
    """从请求提取 (group_id, bot_id, user_id)，缺省补 default.

    字段来源（先到先得，经 :func:`normalize_routing_identity` 统一解 list/空串）：
    - 请求顶层 ``user_id`` / ``group_id``|``chat_id`` / ``bot_id``|``app_id``
    - ``metadata`` 顶层 ``user_id`` + ``metadata.routing``（E2A 权威落点）
    - ``metadata.query``（Web HTTP 入口会把 X-Group/Bot/User 放进 query 袋；
      若 E2A 途中丢了 ``routing``，仍可从这里恢复，避免落到
      ``default::default::{user}`` 错租户）
    """
    from jiuwenswarm.common.request_identity import (
        normalize_routing_identity,
        web_routing_identity,
    )

    meta = getattr(request, "metadata", None)
    meta = meta if isinstance(meta, dict) else {}
    query = meta.get("query") if isinstance(meta.get("query"), dict) else None
    identity = normalize_routing_identity(
        {
            "user_id": getattr(request, "user_id", None),
            "group_id": getattr(request, "group_id", None)
            or getattr(request, "chat_id", None),
            "bot_id": getattr(request, "bot_id", None)
            or getattr(request, "app_id", None),
        },
        web_routing_identity(meta),
        query,
    )
    return (
        normalize_part(identity.get("group_id")),
        normalize_part(identity.get("bot_id")),
        normalize_part(identity.get("user_id")),
    )


def extract_tenant_key(request: Any) -> str:
    """从请求三元组拼出 tenant_key（有则用、无则 default）.

    优先委托企业平面 TenantStrategy；无平面（个人版）固定默认键。
    """
    try:
        from jiuwenswarm.enterprise import get_enterprise_plane

        plane = get_enterprise_plane()
        if plane is not None:
            return plane.tenant.extract_tenant_key(request)
    except Exception:
        logger.debug(
            "[tenant_identity] enterprise plane extract_tenant_key failed",
            exc_info=True,
        )
    if not is_enterprise():
        return build_tenant_key()
    group_id, bot_id, user_id = extract_triple(request)
    return build_tenant_key(group_id, bot_id, user_id)


@contextmanager
def bind_control_tenant(request: Any) -> Iterator[str]:
    """Bind ``TenantContext`` for Front control-plane disk I/O.

    Personal: key is default and path CVs stay unbound (develop layout).
    Enterprise: sessions/history/project reads use the same workspace as chat.
    """
    tenant_key = extract_tenant_key(request)
    with TenantContext.scoped(tenant_key):
        yield tenant_key
