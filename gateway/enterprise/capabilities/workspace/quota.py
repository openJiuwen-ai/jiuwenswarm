# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway 工作区配额：策略选路 + usage 缓存写入.

状态计算等纯函数留在本模块（不依赖 ``jiuwenswarm.enterprise``），
与 AgentServer 侧 ``jiuwenswarm.enterprise.capabilities.workspace.quota`` 语义对齐。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Literal

from gateway.enterprise.config.manager_config.infrastructure.utils import utc_now

logger = logging.getLogger(__name__)

DEFAULT_FALLBACK_LIMIT_BYTES = 10 * 1024**4
FALLBACK_POLICY_ID = "local_default"
FALLBACK_LIMIT_ENV = "AGENT_WORKSPACE_QUOTA_DEFAULT_LIMIT_BYTES"
UNLIMITED_LIMIT_BYTES = -1

QuotaStatus = Literal["ok", "warn", "block"]


@dataclass(frozen=True)
class QuotaSnapshot:
    used_bytes: int
    limit_bytes: int
    percent: float
    status: QuotaStatus
    source_policy_id: str
    soft_percent: int = 80
    hard_percent: int = 100
    unlimited: bool = False


def _is_unlimited_limit(limit_bytes: int) -> bool:
    try:
        return int(limit_bytes) == UNLIMITED_LIMIT_BYTES
    except (TypeError, ValueError):
        return False


def fallback_limit_bytes() -> int:
    """无策略时的默认配额。``0`` / 负数 / 非法值回落 10 TiB。"""
    raw = os.getenv(FALLBACK_LIMIT_ENV, "").strip()
    if not raw:
        return DEFAULT_FALLBACK_LIMIT_BYTES
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "[workspace.quota] invalid %s=%r; using default %s",
            FALLBACK_LIMIT_ENV,
            raw,
            DEFAULT_FALLBACK_LIMIT_BYTES,
        )
        return DEFAULT_FALLBACK_LIMIT_BYTES
    if value <= 0:
        logger.warning(
            "[workspace.quota] non-positive %s=%r; using default %s",
            FALLBACK_LIMIT_ENV,
            raw,
            DEFAULT_FALLBACK_LIMIT_BYTES,
        )
        return DEFAULT_FALLBACK_LIMIT_BYTES
    return value


def coerce_policy_int(value: Any, default: int) -> int:
    """把策略字段转成 int。缺失或非数值用 ``default``；``0`` 保留。"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning(
            "[workspace.quota] invalid policy integer %r; using default %s",
            value,
            default,
        )
        return default


def compute_quota_status(
    used_bytes: int,
    limit_bytes: int,
    *,
    soft_percent: int = 80,
    hard_percent: int = 100,
    source_policy_id: str = "",
) -> QuotaSnapshot:
    """由 used / limit / 阈值计算 ``ok`` / ``warn`` / ``block``。"""
    used = max(0, int(used_bytes))
    soft = int(soft_percent) if soft_percent is not None else 80
    hard = int(hard_percent) if hard_percent is not None else 100
    if hard <= soft:
        hard = soft + 1

    raw_limit = int(limit_bytes)
    if _is_unlimited_limit(raw_limit):
        return QuotaSnapshot(
            used_bytes=used,
            limit_bytes=UNLIMITED_LIMIT_BYTES,
            percent=0.0,
            status="ok",
            source_policy_id=str(source_policy_id or ""),
            soft_percent=soft,
            hard_percent=hard,
            unlimited=True,
        )

    limit = max(0, raw_limit)
    if limit <= 0:
        percent = 0.0 if used == 0 else 100.0
        status: QuotaStatus = "block"
    else:
        percent = (used / limit) * 100.0
        soft_bytes = limit * soft / 100.0
        hard_bytes = limit * hard / 100.0
        if used >= hard_bytes:
            status = "block"
        elif used >= soft_bytes:
            status = "warn"
        else:
            status = "ok"
    return QuotaSnapshot(
        used_bytes=used,
        limit_bytes=limit,
        percent=round(percent, 4),
        status=status,
        source_policy_id=str(source_policy_id or ""),
        soft_percent=soft,
        hard_percent=hard,
        unlimited=False,
    )


async def list_enabled_policies() -> list[dict[str, Any]]:
    """读取本集群启用中的配额策略；个人版 / 未装配时返回空。"""
    try:
        from gateway.config.enterprise.access import (
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
    from gateway.config.enterprise.expressions import matches

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
        coerce_policy_int(policy.get("limit_bytes"), 0),
        soft_percent=coerce_policy_int(policy.get("soft_percent"), 80),
        hard_percent=coerce_policy_int(policy.get("hard_percent"), 100),
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
        from gateway.config.enterprise.access import (
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
