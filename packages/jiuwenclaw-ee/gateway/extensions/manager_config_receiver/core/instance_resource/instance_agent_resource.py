# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""实例 Agent 资源：将 Manager 下发的 instance_agent_resource 写入 Gateway 本地库。"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.gateway.config.enterprise.repository import EnterpriseRecordRepository
from jiuwenswarm.gateway.config.enterprise.tables.instance_resource_models import INSTANCE_AGENT_RESOURCE_TABLE_DEF
from ...infrastructure.repository_access import require_enterprise_repository
from ...infrastructure.utils import parse_iso_datetime, utc_now
from ...schemas.instance_resource_schemas import InstanceAgentResourceUpsertRequest

_TABLE = INSTANCE_AGENT_RESOURCE_TABLE_DEF.table_name
logger = logging.getLogger(__name__)


def _normalize_resource_id(resource_id: Any) -> str:
    normalized = str(resource_id or "").strip()
    if not normalized:
        raise ValueError("resource_id is required")
    if len(normalized) > 100:
        raise ValueError("resource_id must be at most 100 characters")
    return normalized


async def _get_row_for_resource(
    repo: EnterpriseRecordRepository,
    resource_id: str,
) -> dict[str, Any] | None:
    rows = await repo.list(filters={"resource_id": _normalize_resource_id(resource_id)}, limit=1)
    return rows[0] if rows else None


def _build_row_from_resource(
    resource: dict[str, Any],
    *,
    now: Any,
) -> dict[str, Any]:
    req = InstanceAgentResourceUpsertRequest.model_validate(resource)
    return {
        "resource_id": _normalize_resource_id(req.resource_id),
        "resource_name": req.resource_name.strip(),
        "resource_desc": req.resource_desc,
        "ref_template_id": req.ref_template_id.strip(),
        "match_expr": req.match_expr if req.match_expr is not None else [],
        "granted_by": req.granted_by,
        "expires_at": req.expires_at,
        "enabled": bool(req.enabled),
        "data": req.data,
        "created_at": now,
        "updated_at": now,
    }


async def _upsert_instance_agent_resource(
    repo: EnterpriseRecordRepository,
    resource: dict[str, Any],
) -> None:
    now = utc_now()
    rid = _normalize_resource_id(resource.get("resource_id"))
    existing = await _get_row_for_resource(repo, rid)
    row_data = _build_row_from_resource(resource, now=now)
    if existing is None:
        await repo.create(row_data)
        return
    created_at = existing.get("created_at")
    if created_at is not None:
        # existing 可能是 ISO 字符串；asyncpg 要求 datetime
        row_data["created_at"] = parse_iso_datetime(created_at) or now
    updates = {
        key: value
        for key, value in row_data.items()
        if key not in ("resource_id",)
    }
    updates["updated_at"] = utc_now()
    await repo.update({"resource_id": rid}, updates)


async def delete_instance_agent_resource(
    repo: EnterpriseRecordRepository,
    resource_id: str,
) -> bool:
    rid = _normalize_resource_id(resource_id)
    existing = await _get_row_for_resource(repo, rid)
    if existing is None:
        return False
    return await repo.delete(resource_id=rid)


class InstanceAgentResourceService:

    async def upsert(
        self,
        resource: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(resource, dict):
            raise ValueError("instance_agent_resources.upsert requires resource object")
        repo = require_enterprise_repository(_TABLE)
        await _upsert_instance_agent_resource(repo, resource)
        result = {"resource_id": _normalize_resource_id(resource.get("resource_id"))}
        logger.info(
            "[ManagerConfigReceiver] instance_agent_resources upsert resource_id=%s",
            result["resource_id"],
        )
        return result

    async def delete(self, resource_id: str) -> None:
        if resource_id is None:
            raise ValueError("instance_agent_resources.delete requires resource_id")
        rid = _normalize_resource_id(resource_id)
        repo = require_enterprise_repository(_TABLE)
        deleted = await delete_instance_agent_resource(repo, rid)
        if not deleted:
            raise ValueError(f"instance agent resource resource_id={rid!r} not found")
        logger.info(
            "[ManagerConfigReceiver] instance_agent_resources delete resource_id=%s",
            rid,
        )


__all__ = ("InstanceAgentResourceService",)
