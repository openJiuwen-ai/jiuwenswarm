# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""安全护栏模板：将 Claw Manager 下发的 permissions_templates 写入 Gateway 本地库。"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.gateway.config.enterprise.repository import EnterpriseRecordRepository
from jiuwenswarm.gateway.config.enterprise.tables.template_models import (
    PERMISSIONS_TEMPLATE_TABLE_DEF,
)

from ...infrastructure.repository_access import require_enterprise_repository
from ...infrastructure.utils import parse_iso_datetime, utc_now
from ...schemas.template_schemas import PermissionsTemplateUpdateRequest

_TABLE = PERMISSIONS_TEMPLATE_TABLE_DEF.table_name
logger = logging.getLogger(__name__)


def _normalize_template_id(template_id: Any) -> str:
    normalized = str(template_id or "").strip()
    if not normalized:
        raise ValueError("template_id is required")
    if len(normalized) > 100:
        raise ValueError("template_id must be at most 100 characters")
    return normalized


def _normalize_body(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("body must be a JSON object")
    return value


async def _get_row_for_instance(
    repo: EnterpriseRecordRepository,
    template_id: str,
) -> dict[str, Any] | None:
    return await repo.get(template_id=_normalize_template_id(template_id))


async def update_permissions_template(
    repo: EnterpriseRecordRepository,
    template_id: str,
    request: PermissionsTemplateUpdateRequest,
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
    if "body" in updates and updates["body"] is not None:
        updates["body"] = _normalize_body(updates["body"])

    if not updates:
        raise ValueError("请求未包含任何可更新的业务字段")

    updates["updated_at"] = utc_now()
    updated = await repo.update({"template_id": tid}, updates)
    if updated is None:
        return None
    return {"template_id": str(updated.get("template_id", tid))}


async def delete_permissions_template(
    repo: EnterpriseRecordRepository,
    template_id: str,
) -> bool:
    tid = _normalize_template_id(template_id)
    existing = await _get_row_for_instance(repo, tid)
    if existing is None:
        return False
    return await repo.delete(template_id=tid)


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
        "enabled": bool(template.get("enabled", True)),
        "body": _normalize_body(template.get("body")),
        "data": template.get("data"),
        "created_at": now,
        "updated_at": now,
    }


async def _upsert_permissions_template_from_sync(
    repo: EnterpriseRecordRepository,
    template: dict[str, Any],
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
        k: v for k, v in row_data.items() if k not in ("template_id",)
    }
    updates["updated_at"] = utc_now()
    await repo.update({"template_id": tid}, updates)


async def _sync_permissions_templates_records(
    repo: EnterpriseRecordRepository,
    templates: list[dict[str, Any]],
) -> dict[str, Any]:
    incoming_ids: set[str] = set()
    synced = 0
    for item in templates:
        if not isinstance(item, dict):
            raise ValueError("permissions_templates.sync templates must be objects")
        tid = _normalize_template_id(item.get("template_id"))
        incoming_ids.add(tid)
        await _upsert_permissions_template_from_sync(repo, item)
        synced += 1
    deleted = 0
    for row in await repo.list():
        tid = str(row.get("template_id") or "")
        if tid and tid not in incoming_ids:
            if await delete_permissions_template(repo, tid):
                deleted += 1
    return {"synced_count": synced, "deleted_count": deleted}


class PermissionsTemplateService:

    async def create(
        self,
        template: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(template, dict):
            raise ValueError("permissions_templates.create requires template object")
        if "body" not in template:
            raise ValueError("permissions_templates.create requires body object")
        repo = require_enterprise_repository(_TABLE)
        await _upsert_permissions_template_from_sync(repo, template)
        result = {
            "template_id": _normalize_template_id(template.get("template_id")),
        }
        logger.info(
            "[ManagerConfigReceiver] permissions_templates create template_id=%s",
            result["template_id"],
        )
        return result

    async def update(
        self,
        template_id: str,
        updates: dict[str, Any],
    ) -> None:
        if template_id is None:
            raise ValueError("permissions_templates.update requires template_id")
        if not isinstance(updates, dict) or not updates:
            raise ValueError("permissions_templates.update requires non-empty updates")
        req = PermissionsTemplateUpdateRequest.model_validate(updates)
        tid = _normalize_template_id(template_id)
        repo = require_enterprise_repository(_TABLE)
        existing = await _get_row_for_instance(repo, tid)
        row = await update_permissions_template(
            repo, tid, req, existing=existing
        )
        if row is None:
            raise ValueError(f"permissions template template_id={tid!r} not found")
        logger.info(
            "[ManagerConfigReceiver] permissions_templates update template_id=%s",
            tid,
        )

    async def delete(self, template_id: str) -> None:
        if template_id is None:
            raise ValueError("permissions_templates.delete requires template_id")
        tid = _normalize_template_id(template_id)
        repo = require_enterprise_repository(_TABLE)
        await delete_permissions_template(repo, tid)
        logger.info(
            "[ManagerConfigReceiver] permissions_templates delete template_id=%s",
            tid,
        )
