# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Per-request Agent resource admission, independent of Runtime pool routing."""
from datetime import datetime, timezone

from jiuwenswarm.common.e2a.models import E2AEnvelope

from jiuwenswarm.gateway.config.enterprise.access import get_enterprise_record_repository
from jiuwenswarm.gateway.config.enterprise.expressions import matches

from .invoke_ids import routing_triple_from_envelope
from .session_route_client import FatalRouteError

# 本单约束发起/继续 Agent 执行的对话入口。会话管理、历史查询与中断
# 沿用各自接口契约，不在这里按前缀扩大授权范围。
CHAT_METHODS = frozenset({
    "chat.send", "chat.resume", "chat.user_answer", "chat.swarmflow_reply",
})


async def authorize_agent(envelope: E2AEnvelope) -> tuple[str, str, str] | None:
    if envelope.method not in CHAT_METHODS:
        return None
    group_id, bot_id, user_id = routing_triple_from_envelope(envelope)
    identity = {"group_id": group_id, "bot_id": bot_id, "user_id": user_id}
    if not all(identity.values()):
        raise FatalRouteError("Agent routing identity is required", code="FORBIDDEN")
    repo = get_enterprise_record_repository("instance_agent_resource")
    if repo is None:
        raise RuntimeError("instance_agent_resource repository is unavailable")
    # Do not cache admission: revocation and expiry apply to the next request.
    row = await repo.get(resource_id=bot_id)
    allowed = False
    # PostgreSQL 读回 bool，MySQL 的布尔编解码器读回 1/0。
    if row is not None and row.get("enabled") == 1:
        try:
            expiry = row.get("expires_at")
            if isinstance(expiry, str):
                expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            if expiry is not None and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            allowed = (expiry is None or expiry > datetime.now(timezone.utc)) and matches(
                row.get("match_expr"), identity
            )
        except (ValueError, TypeError, AttributeError):
            allowed = False
    if not allowed:
        raise FatalRouteError("Agent access denied", code="FORBIDDEN")
    return group_id, bot_id, user_id
