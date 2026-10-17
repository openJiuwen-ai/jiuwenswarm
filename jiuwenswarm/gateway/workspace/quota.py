# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway 工作区配额：策略选路 + usage 缓存写入。"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.workspace.quota import (
    FALLBACK_POLICY_ID,
    QuotaSnapshot,
    compute_quota_status,
    fallback_limit_bytes,
)
from jiuwenswarm.infrastructure.utils import utc_now

logger = logging.getLogger(__name__)


async def list_enabled_policies() -> list[dict[str, Any]]:
    """读取本集群启用中的配额策略；个人版 / 未装配时返回空。"""
    try:
        from jiuwenswarm.gateway.config.enterprise.access import (
            get_enterprise_record_repository,
        )
    except Exception:  # noqa: BLE001
        return []
    repo = get_enterprise_record_repository("workspace_quota_policy")
    if repo is None:
        return []
    try:
        rows = await repo.list(filters={"enabled": True}, order_by="priority", limit=500)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[workspace.quota] list policies failed: %s", exc)
        return []
    return [row for row in rows if isinstance(row, dict)]


def pick_policy(
    policies: list[dict[str, Any]],
    *,
    user_id: str,
    group_id: str,
    bot_id: str,
) -> dict[str, Any] | None:
    """命中 enabled 策略中 priority 最小的一条。"""
    from jiuwenswarm.gateway.config.enterprise.expressions import matches

    identity = {
        "user_id": str(user_id or ""),
        "group_id": str(group_id or ""),
        "bot_id": str(bot_id or ""),
    }
    hits: list[tuple[int, dict[str, Any]]] = []
    for row in policies:
        if not bool(row.get("enabled", True)):
            continue
        expr = row.get("match_expr")
        try:
            if not matches(expr, identity):
                continue
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[workspace.quota] skip invalid match_expr policy_id=%s: %s",
                row.get("policy_id"),
                exc,
            )
            continue
        try:
            priority = int(row.get("priority", 0))
        except (TypeError, ValueError):
            priority = 0
        hits.append((priority, row))
    if not hits:
        return None
    hits.sort(key=lambda item: item[0])
    return hits[0][1]


async def resolve_quota_snapshot(
    used_bytes: int,
    *,
    user_id: str,
    group_id: str,
    bot_id: str,
) -> QuotaSnapshot:
    policies = await list_enabled_policies()
    policy = pick_policy(policies, user_id=user_id, group_id=group_id, bot_id=bot_id)
    if policy is None:
        # 无命中策略：环境变量 AGENT_WORKSPACE_QUOTA_DEFAULT_LIMIT_BYTES，默认 10 TiB。
        return compute_quota_status(
            used_bytes,
            fallback_limit_bytes(),
            soft_percent=80,
            hard_percent=100,
            source_policy_id=FALLBACK_POLICY_ID,
        )
    return compute_quota_status(
        used_bytes,
        int(policy.get("limit_bytes", 0)),
        soft_percent=int(policy.get("soft_percent") or 80),
        hard_percent=int(policy.get("hard_percent") or 100),
        source_policy_id=str(policy.get("policy_id") or ""),
    )


async def upsert_usage_cache(
    *,
    user_id: str,
    group_id: str,
    bot_id: str,
    used_bytes: int,
) -> None:
    """按三元组 upsert Gateway ``workspace_quota_usage``；未装配时静默跳过。"""
    try:
        from jiuwenswarm.gateway.config.enterprise.access import (
            get_enterprise_record_repository,
        )
    except Exception:  # noqa: BLE001
        return
    repo = get_enterprise_record_repository("workspace_quota_usage")
    if repo is None:
        return

    uid = str(user_id or "").strip()
    gid = str(group_id or "").strip()
    bid = str(bot_id or "").strip()
    if not uid or not bid:
        logger.debug("[workspace.usage] skip cache upsert: missing user_id/bot_id")
        return

    now = utc_now()
    used = max(0, int(used_bytes))
    key = {"user_id": uid, "group_id": gid, "bot_id": bid}
    try:
        existing = await repo.get(key)
        if existing is None:
            await repo.create(
                {
                    **key,
                    "used_bytes": used,
                    "reported_at": now,
                    "data": None,
                    "created_at": now,
                    "created_by": None,
                    "updated_at": now,
                    "updated_by": None,
                }
            )
            return
        await repo.update(
            key,
            {
                "used_bytes": used,
                "reported_at": now,
                "updated_at": now,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[workspace.usage] upsert cache failed: %s", exc)
