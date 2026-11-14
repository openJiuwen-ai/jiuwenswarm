# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Per-request Agent resource admission, independent of Runtime pool routing."""
import logging
from datetime import datetime, timezone

from e2a.models import E2AEnvelope
from jiuwenswarm.extensions.runtime_management_extension.invoke_ids import (
    routing_triple_from_envelope,
)
from jiuwenswarm.extensions.runtime_management_extension.session_route_client import (
    FatalRouteError,
)

logger = logging.getLogger(__name__)
_enterprise_absent_logged = False

# 对话入口，以及专家、专家团的查看、安装和本地管理。会话历史不在这里，
# 也不按路径前缀扩大。
CHAT_METHODS = frozenset({
    "chat.send", "chat.resume", "chat.user_answer", "chat.swarmflow_reply",
})
_EXPERT_KINDS = ("agent_templates", "agent_groups")
_EXPERT_ACTIONS = (
    "list", "show", "file.list", "file.read", "install", "uninstall",
    "create", "import_local",
)
EXPERT_PACKAGE_METHODS = frozenset(
    f"{kind}.{action}" for kind in _EXPERT_KINDS for action in _EXPERT_ACTIONS
) | frozenset({
    "agent_templates.update", "agent_templates.delete",
})
ADMITTED_METHODS = CHAT_METHODS | EXPERT_PACKAGE_METHODS


def _enterprise_checkers():
    """企业库不在本分支时返回空，聊天请求不因此被挡住。"""
    global _enterprise_absent_logged
    try:
        from agent_gateway.config.enterprise.access import (
            get_enterprise_record_repository,
        )
        from agent_gateway.config.enterprise.expressions import matches
    except ImportError:
        if not _enterprise_absent_logged:
            _enterprise_absent_logged = True
            logger.info("[RuntimeManagement] 企业访问校验不在本分支，聊天请求直接通过")
        return None
    return get_enterprise_record_repository, matches


async def authorize_agent(envelope: E2AEnvelope) -> tuple[str, str, str] | None:
    if envelope.method not in ADMITTED_METHODS:
        return None
    checkers = _enterprise_checkers()
    if checkers is None:
        return None
    get_enterprise_record_repository, matches = checkers
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
    # PostgreSQL 读回 bool，MySQL 布尔编解码器读回 1/0。
    enabled = row.get("enabled") if row is not None else None
    if row is not None and enabled in (1, True, "1"):
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
