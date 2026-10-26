# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""Agent 模板：将 Manager 下发的 agent_templates 写入 Gateway 本地库。"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.gateway.config.enterprise.repository import EnterpriseRecordRepository
from jiuwenswarm.gateway.config.enterprise.tables.template_models import AGENT_TEMPLATE_TABLE_DEF

from ...infrastructure.repository_access import require_enterprise_repository
from ...infrastructure.utils import (
    normalize_template_ref,
    parse_iso_datetime,
    utc_now,
)
from ...schemas.template_schemas import AgentTemplateUpdateRequest

_TABLE = AGENT_TEMPLATE_TABLE_DEF.table_name
logger = logging.getLogger(__name__)


def _normalize_template_id(template_id: Any) -> str:
    normalized = str(template_id or "").strip()
    if not normalized:
        raise ValueError("template_id is required")
    if len(normalized) > 100:
        raise ValueError("template_id must be at most 100 characters")
    return normalized


async def _get_row_for_instance(
    repo: EnterpriseRecordRepository,
    template_id: str,
) -> dict[str, Any] | None:
    return await repo.get(template_id=_normalize_template_id(template_id))


def _build_row_from_template(
    template: dict[str, Any],
    *,
    now: Any,
) -> dict[str, Any]:
    template_uuid = _normalize_template_id(template.get("template_id"))
    return {
        "template_id": template_uuid,
        "template_name": str(template["template_name"]).strip(),
        "description": template.get("description"),
        "agent_tags": template.get("agent_tags"),
        "template_ref": normalize_template_ref(template.get("template_ref") or {}),
        "enabled": bool(template.get("enabled", True)),
        "data": template.get("data"),
        "created_at": now,
        "updated_at": now,
    }


async def _upsert_agent_template_from_sync(
    repo: EnterpriseRecordRepository,
    template: dict[str, Any]
) -> None:
    now = utc_now()
    tid = _normalize_template_id(template.get("template_id"))
    existing = await _get_row_for_instance(repo, tid)
    row_data = _build_row_from_template(template, now=now)
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
        if key not in ("template_id",)
    }
    updates["updated_at"] = utc_now()
    await repo.update({"template_id": tid}, updates)


async def update_agent_template(
    repo: EnterpriseRecordRepository,
    template_id: str,
    request: AgentTemplateUpdateRequest,
    *,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    tid = _normalize_template_id(template_id)
    row = existing if existing is not None else await _get_row_for_instance(repo, tid)
    if row is None:
        return None

    updates = request.model_dump(exclude_unset=True)
    if "template_name" in updates and updates["template_name"] is not None:
        updates["template_name"] = updates["template_name"].strip()
    if "template_ref" in updates and updates["template_ref"] is not None:
        updates["template_ref"] = normalize_template_ref(updates["template_ref"])
    if not updates:
        raise ValueError("请求未包含任何可更新的业务字段")

    updates["updated_at"] = utc_now()
    updated = await repo.update({"template_id": tid}, updates)
    if updated is None:
        return None
    return {"template_id": str(updated.get("template_id", tid))}


async def delete_agent_template(
    repo: EnterpriseRecordRepository,
    template_id: str,
) -> bool:
    tid = _normalize_template_id(template_id)
    existing = await _get_row_for_instance(repo, tid)
    if existing is None:
        return False
    return await repo.delete(template_id=tid)


class AgentTemplateService:

    async def create(
        self,
        template: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(template, dict):
            raise ValueError("agent_templates.create requires template object")
        repo = require_enterprise_repository(_TABLE)
        await _upsert_agent_template_from_sync(
            repo, template
        )
        result = {"template_id": _normalize_template_id(template.get("template_id"))}
        logger.info(
            "[ManagerConfigReceiver] agent_templates create template_id=%s",
            result["template_id"],
        )
        return result

    async def update(
        self,
        template_id: str,
        updates: dict[str, Any],
    ) -> None:
        if template_id is None:
            raise ValueError("agent_templates.update requires template_id")
        if not isinstance(updates, dict) or not updates:
            raise ValueError("agent_templates.update requires non-empty updates")
        req = AgentTemplateUpdateRequest.model_validate(updates)
        tid = _normalize_template_id(template_id)
        repo = require_enterprise_repository(_TABLE)
        row = await update_agent_template(repo, tid, req)
        if row is None:
            raise ValueError(f"agent template template_id={tid!r} not found")
        logger.info(
            "[ManagerConfigReceiver] agent_templates update template_id=%s",
            tid,
        )

    async def delete(self, template_id: str) -> None:
        if template_id is None:
            raise ValueError("agent_templates.delete requires template_id")
        tid = _normalize_template_id(template_id)
        repo = require_enterprise_repository(_TABLE)
        await delete_agent_template(repo, tid)
        logger.info(
            "[ManagerConfigReceiver] agent_templates delete template_id=%s",
            tid,
        )


__all__ = ("AgentTemplateService",)
