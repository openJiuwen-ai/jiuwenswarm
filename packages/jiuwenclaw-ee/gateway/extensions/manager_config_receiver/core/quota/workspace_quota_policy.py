# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""工作区配额策略：Manager 按 policy_id upsert / 删除 Gateway 副本。"""

from __future__ import annotations

import json
from typing import Any

from jiuwenswarm.gateway.config.enterprise.expressions import validate_match_expr
from jiuwenswarm.gateway.config.enterprise.tables.quota_models import (
    WORKSPACE_QUOTA_POLICY_TABLE_DEF,
)

from ...infrastructure.repository_access import require_enterprise_repository
from ...infrastructure.utils import parse_iso_datetime, utc_now
from ...schemas.quota_schemas import PolicyUpsertRequest

_TABLE = WORKSPACE_QUOTA_POLICY_TABLE_DEF.table_name
_PAGE = 500


def _canonical_match_expr(value: Any) -> Any:
    """与 Manager 入库形态对齐：全匹配为 ``[]``，单条为字符串，多条 OR 为字符串列表。"""
    if value is None:
        return []
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                parts.append(text)
        if not parts:
            return []
        if len(parts) == 1:
            return _canonical_string(parts[0])
        return parts
    if isinstance(value, str):
        return _canonical_string(value.strip())
    text = str(value).strip()
    return _canonical_string(text) if text else []


def _canonical_string(text: str) -> Any:
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        else:
            if isinstance(parsed, list):
                return _canonical_match_expr(parsed)
    return text


def _match_key(expr: Any) -> str:
    return json.dumps(_canonical_match_expr(expr), ensure_ascii=False, separators=(",", ":"))


def _is_full_match(expr: Any) -> bool:
    canon = _canonical_match_expr(expr)
    if canon == []:
        return True
    if not isinstance(canon, str):
        return False
    return not any(marker in canon for marker in ("==", "!=", " in "))


def _validated_expr(value: Any) -> Any:
    canon = _canonical_match_expr(value)
    validate_match_expr(canon)
    return canon


class WorkspaceQuotaPolicyService:
    async def upsert(self, body: dict[str, Any]) -> None:
        if not isinstance(body, dict):
            raise ValueError("workspace quota policy upsert requires an object")
        req = PolicyUpsertRequest.model_validate(body)
        expr = _validated_expr(req.match_expr)
        pid = req.policy_id.strip()
        enabled = bool(req.enabled)
        priority = int(req.priority)
        repo = require_enterprise_repository(_TABLE)
        existing = await repo.get(policy_id=pid)
        if enabled:
            await self._assert_unique(repo, expr, priority, exclude_policy_id=pid)
        now = utc_now()
        source = req.source
        source_order_num = req.source_order_num
        if existing is None:
            await repo.create(
                {
                    "policy_id": pid,
                    "policy_name": req.policy_name,
                    "policy_desc": req.policy_desc,
                    "match_expr": expr,
                    "priority": priority,
                    "limit_bytes": int(req.limit_bytes),
                    "soft_percent": int(req.soft_percent),
                    "hard_percent": int(req.hard_percent),
                    "source": source,
                    "source_order_num": source_order_num,
                    "enabled": enabled,
                    "data": None,
                    "created_at": now,
                    "created_by": None,
                    "updated_at": now,
                    "updated_by": None,
                }
            )
            return
        created_at = parse_iso_datetime(existing.get("created_at")) or now
        updated = await repo.update(
            {"policy_id": pid},
            {
                "policy_name": req.policy_name,
                "policy_desc": req.policy_desc,
                "match_expr": expr,
                "priority": priority,
                "limit_bytes": int(req.limit_bytes),
                "soft_percent": int(req.soft_percent),
                "hard_percent": int(req.hard_percent),
                "source": source,
                "source_order_num": source_order_num,
                "enabled": enabled,
                "updated_at": now,
                "created_at": created_at,
            },
        )
        if updated is None:
            raise ValueError(f"failed to upsert workspace quota policy id={pid!r}")

    async def delete(self, policy_id: str) -> None:
        pid = str(policy_id or "").strip()
        if not pid:
            raise ValueError("policy_id is required")
        repo = require_enterprise_repository(_TABLE)
        existing = await repo.get(policy_id=pid)
        if existing is None:
            return
        await repo.delete(policy_id=pid)

    async def _assert_unique(
        self,
        repo: Any,
        match_expr: Any,
        priority: int,
        *,
        exclude_policy_id: str,
    ) -> None:
        rows = await _list_all(repo)
        key = _match_key(match_expr)
        full = _is_full_match(match_expr)
        for row in rows:
            if not bool(row.get("enabled", True)):
                continue
            pid = str(row.get("policy_id") or "")
            if pid == exclude_policy_id:
                continue
            # 与 Manager 对齐：负数域（审批 -1）可多条并存；非负 priority 仍唯一
            if int(priority) >= 0 and int(row.get("priority") or 0) == int(priority):
                raise ValueError("priority already used by an enabled policy")
            if _match_key(row.get("match_expr")) == key:
                raise ValueError("match_expr already used by an enabled policy")
            if full and _is_full_match(row.get("match_expr")):
                raise ValueError("full-match policy already exists")


async def _list_all(repo: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        batch = await repo.list(limit=_PAGE, offset=offset)
        rows.extend(batch)
        if len(batch) < _PAGE:
            return rows
        offset += _PAGE
