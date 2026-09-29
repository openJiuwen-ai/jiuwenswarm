# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""工作区用量：只读本集群 ``workspace_quota_usage`` 缓存，供 Manager 同步。"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.workspace.quota import (
    FALLBACK_POLICY_ID,
    compute_quota_status,
    fallback_limit_bytes,
)
from jiuwenswarm.gateway.config.enterprise.tables.quota_models import (
    WORKSPACE_QUOTA_USAGE_TABLE_DEF,
)
from jiuwenswarm.gateway.workspace.quota import list_enabled_policies, pick_policy

from ...infrastructure.repository_access import require_enterprise_repository
from ...infrastructure.utils import format_ts

_TABLE = WORKSPACE_QUOTA_USAGE_TABLE_DEF.table_name
_PAGE = 500
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _format_reported_at(value: Any) -> str:
    text = format_ts(value)
    if not text:
        return ""
    return text.replace("+00:00", "Z")


class WorkspaceQuotaUsageService:
    """读 Gateway 用量缓存；合成字段现算，不写回用量表。"""

    async def list_usage(
        self,
        *,
        user_id: str | None = None,
        group_id: str | None = None,
        bot_id: str | None = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        page_size = min(max(int(limit or _DEFAULT_LIMIT), 1), _MAX_LIMIT)
        filters: dict[str, Any] = {}
        uid = (user_id or "").strip()
        if uid:
            filters["user_id"] = uid
        # group_id 显式传入（含空串）才过滤；None 表示不过滤
        if group_id is not None:
            filters["group_id"] = group_id
        bid = (bot_id or "").strip()
        if bid:
            filters["bot_id"] = bid

        rows = await self._list_all(filters or None)
        policies = await list_enabled_policies()
        items: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item_user = _as_str(row.get("user_id")).strip()
            item_group = _as_str(row.get("group_id"))
            item_bot = _as_str(row.get("bot_id")).strip()
            if not item_user or not item_bot:
                continue
            used = max(0, _as_int(row.get("used_bytes")))
            policy = pick_policy(
                policies,
                user_id=item_user,
                group_id=item_group,
                bot_id=item_bot,
            )
            if policy is None:
                snap = compute_quota_status(
                    used,
                    fallback_limit_bytes(),
                    soft_percent=80,
                    hard_percent=100,
                    source_policy_id=FALLBACK_POLICY_ID,
                )
            else:
                snap = compute_quota_status(
                    used,
                    _as_int(policy.get("limit_bytes"), 0),
                    soft_percent=_as_int(policy.get("soft_percent"), 80),
                    hard_percent=_as_int(policy.get("hard_percent"), 100),
                    source_policy_id=_as_str(policy.get("policy_id")),
                )
            items.append(
                {
                    "user_id": item_user,
                    "group_id": item_group,
                    "bot_id": item_bot,
                    "used_bytes": snap.used_bytes,
                    "limit_bytes": snap.limit_bytes,
                    "source_policy_id": snap.source_policy_id,
                    "status": snap.status,
                    "reported_at": _format_reported_at(row.get("reported_at")),
                }
            )

        items.sort(
            key=lambda it: (
                -int(it["used_bytes"]),
                it["user_id"],
                it["group_id"],
                it["bot_id"],
            )
        )
        return {"items": items[:page_size]}

    async def _list_all(self, filters: dict[str, Any] | None) -> list[dict[str, Any]]:
        repo = require_enterprise_repository(_TABLE)
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            batch = await repo.list(
                filters=filters,
                limit=_PAGE,
                offset=offset,
            )
            rows.extend(batch)
            if len(batch) < _PAGE:
                return rows
            offset += _PAGE
