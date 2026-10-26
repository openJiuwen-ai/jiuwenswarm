# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""Embedding 模板：将 Manager 下发的 embedding_templates 写入 Gateway 本地库。"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.gateway.config.enterprise.repository import EnterpriseRecordRepository
from jiuwenswarm.gateway.config.enterprise.tables.template_models import EMBEDDING_TEMPLATE_TABLE_DEF

from ...infrastructure.repository_access import require_enterprise_repository
from ...infrastructure.utils import parse_iso_datetime, utc_now
from ...schemas.template_schemas import EmbeddingTemplateUpdateRequest

_TABLE = EMBEDDING_TEMPLATE_TABLE_DEF.table_name
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


async def update_embedding_template(
    repo: EnterpriseRecordRepository,
    template_id: str,
    request: EmbeddingTemplateUpdateRequest,
    *,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    tid = _normalize_template_id(template_id)
    row = existing if existing is not None else await _get_row_for_instance(repo, tid)
    if row is None:
        return None

    updates = request.model_dump(exclude_unset=True)
    for field in ("template_name", "api_base", "model_id", "model_provider"):
        if field in updates and updates[field] is not None:
            updates[field] = updates[field].strip()
    if not updates:
        raise ValueError("请求未包含任何可更新的业务字段")

    updates["updated_at"] = utc_now()
    updated = await repo.update({"template_id": tid}, updates)
    if updated is None:
        return None
    return {"template_id": str(updated.get("template_id", tid))}


async def delete_embedding_template(
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
    template_name = (
        template["template_name"] if "template_name" in template else template["name"]
    )
    return {
        "template_id": template_uuid,
        "template_name": str(template_name).strip(),
        "description": template.get("description"),
        "embed_tags": template.get("embed_tags"),
        "api_base": str(template["api_base"]).strip(),
        "api_key": template["api_key"],
        "model_id": str(template["model_id"]).strip(),
        "model_provider": str(template["model_provider"]).strip(),
        "parameters": template.get("parameters"),
        "client_config": template.get("client_config"),
        "enabled": bool(template.get("enabled", True)),
        "data": template.get("data"),
        "created_at": now,
        "updated_at": now,
    }


async def _upsert_embedding_template_from_sync(
    repo: EnterpriseRecordRepository,
    template: dict[str, Any]
) -> None:
    now = utc_now()
    tid = _normalize_template_id(template.get("template_id"))
    existing = await _get_row_for_instance(repo, tid)
    row_data = _build_row_from_template(
        template, now=now
    )
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


async def _sync_embedding_templates_records(
    repo: EnterpriseRecordRepository,
    templates: list[dict[str, Any]]
) -> dict[str, Any]:
    incoming_ids: set[str] = set()
    synced = 0
    for item in templates:
        if not isinstance(item, dict):
            raise ValueError("embedding_templates.sync templates must be objects")
        tid = _normalize_template_id(item.get("template_id"))
        incoming_ids.add(tid)
        await _upsert_embedding_template_from_sync(
            repo, item
        )
        synced += 1

    deleted = 0
    for row in await repo.list():
        tid = str(row.get("template_id") or "")
        if tid and tid not in incoming_ids:
            if await delete_embedding_template(repo, tid):
                deleted += 1
    return {"synced_count": synced, "deleted_count": deleted}


class EmbeddingTemplateService:

    async def create(
        self,
        template: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(template, dict):
            raise ValueError("embedding_templates.create requires template object")
        repo = require_enterprise_repository(_TABLE)
        await _upsert_embedding_template_from_sync(
            repo, template
        )
        result = {
            "template_id": _normalize_template_id(template.get("template_id")),
        }
        logger.info(
            "[ManagerConfigReceiver] embedding_templates create template_id=%s",
            result["template_id"],
        )
        return result

    async def update(
        self,
        template_id: str,
        updates: dict[str, Any],
    ) -> None:
        if template_id is None:
            raise ValueError("embedding_templates.update requires template_id")
        if not isinstance(updates, dict) or not updates:
            raise ValueError("embedding_templates.update requires non-empty updates")
        request = EmbeddingTemplateUpdateRequest.model_validate(updates)
        tid = _normalize_template_id(template_id)
        repo = require_enterprise_repository(_TABLE)
        row = await update_embedding_template(repo, tid, request)
        if row is None:
            raise ValueError(f"embedding template template_id={tid!r} not found")
        logger.info(
            "[ManagerConfigReceiver] embedding_templates update template_id=%s",
            tid,
        )

    async def delete(self, template_id: str) -> None:
        if template_id is None:
            raise ValueError("embedding_templates.delete requires template_id")
        tid = _normalize_template_id(template_id)
        repo = require_enterprise_repository(_TABLE)
        await delete_embedding_template(repo, tid)
        logger.info(
            "[ManagerConfigReceiver] embedding_templates delete template_id=%s",
            tid,
        )
