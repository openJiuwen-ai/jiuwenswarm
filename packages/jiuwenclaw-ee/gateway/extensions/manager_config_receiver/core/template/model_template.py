# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""模型模板：将 Claw Manager 下发的 model_templates 写入 Gateway 本地库。"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.gateway.config.enterprise.repository import EnterpriseRecordRepository
from jiuwenswarm.gateway.config.enterprise.tables.template_models import MODEL_TEMPLATE_TABLE_DEF

from ...infrastructure.repository_access import require_enterprise_repository
from ...infrastructure.utils import parse_iso_datetime, utc_now
from ...schemas.template_schemas import ModelTemplateUpdateRequest

_TABLE = MODEL_TEMPLATE_TABLE_DEF.table_name
_ALLOWED_MODEL_TYPES = frozenset({"default", "video", "audio", "vision", "image_gen"})
logger = logging.getLogger(__name__)


def _normalize_template_id(template_id: Any) -> str:
    normalized = str(template_id or "").strip()
    if not normalized:
        raise ValueError("template_id is required")
    if len(normalized) > 100:
        raise ValueError("template_id must be at most 100 characters")
    return normalized


def _normalize_model_types(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value] if value.strip() else []
    elif isinstance(value, list):
        items = value
    else:
        raise ValueError("model_type must be a list of strings")
    normalized: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ValueError("model_type must be a list of strings")
        text = item.strip()
        if not text or text in normalized:
            continue
        if text not in _ALLOWED_MODEL_TYPES:
            raise ValueError(
                f"model_type entries must be in {sorted(_ALLOWED_MODEL_TYPES)}, got {text!r}"
            )
        normalized.append(text)
    return normalized


async def _get_row_for_instance(
    repo: EnterpriseRecordRepository,
    template_id: str,
) -> dict[str, Any] | None:
    return await repo.get(template_id=_normalize_template_id(template_id))


async def update_model_template(
    repo: EnterpriseRecordRepository,
    template_id: str,
    request: ModelTemplateUpdateRequest,
    *,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    tid = _normalize_template_id(template_id)
    row = existing if existing is not None else await _get_row_for_instance(repo, tid)
    if row is None:
        return None

    updates = request.model_dump(exclude_unset=True)
    if "model_type" in updates and updates["model_type"] is not None:
        updates["model_type"] = _normalize_model_types(updates["model_type"])
    if "template_name" in updates and updates["template_name"] is not None:
        updates["template_name"] = updates["template_name"].strip()
    if "api_base" in updates and updates["api_base"] is not None:
        updates["api_base"] = updates["api_base"].strip()
    if "model_id" in updates and updates["model_id"] is not None:
        updates["model_id"] = updates["model_id"].strip()
    if "model_provider" in updates and updates["model_provider"] is not None:
        updates["model_provider"] = updates["model_provider"].strip()

    if not updates:
        raise ValueError("请求未包含任何可更新的业务字段")

    updates["updated_at"] = utc_now()
    updated = await repo.update({"template_id": tid}, updates)
    if updated is None:
        return None
    return {"template_id": str(updated.get("template_id", tid))}


async def delete_model_template(
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
    model_type = _normalize_model_types(template.get("model_type"))
    return {
        "template_id": template_uuid,
        "template_name": str(template["template_name"]).strip(),
        "description": template.get("description"),
        "model_type": model_type,
        "model_tags": template.get("model_tags"),
        "api_base": str(template["api_base"]).strip(),
        "api_key": template["api_key"],
        "model_id": str(template["model_id"]).strip(),
        "model_provider": str(template["model_provider"]).strip(),
        "parameters": template.get("parameters"),
        "timeout": int(template.get("timeout", 60)),
        "retry_count": int(template.get("retry_count", 3)),
        "enable_streaming": bool(template.get("enable_streaming", True)),
        "enable_function_calling": bool(template.get("enable_function_calling", True)),
        "verify_ssl": bool(template.get("verify_ssl", False)),
        "enabled": bool(template.get("enabled", True)),
        "data": template.get("data"),
        "created_at": now,
        "updated_at": now,
    }


async def _upsert_model_template_from_sync(
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
        key: value for key, value in row_data.items() if key not in ("template_id",)
    }
    updates["updated_at"] = utc_now()
    await repo.update({"template_id": tid}, updates)


async def _sync_model_templates_records(
    repo: EnterpriseRecordRepository,
    templates: list[dict[str, Any]]
) -> dict[str, Any]:
    incoming_ids: set[str] = set()
    synced = 0
    for item in templates:
        if not isinstance(item, dict):
            raise ValueError("model_templates.sync templates must be objects")
        tid = _normalize_template_id(item.get("template_id"))
        incoming_ids.add(tid)
        await _upsert_model_template_from_sync(repo, item)
        synced += 1
    deleted = 0
    for row in await repo.list():
        tid = str(row.get("template_id") or "")
        if tid and tid not in incoming_ids:
            if await delete_model_template(repo, tid):
                deleted += 1
    return {"synced_count": synced, "deleted_count": deleted}


class ModelTemplateService:

    async def create(
        self,
        template: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(template, dict):
            raise ValueError("model_templates.create requires template object")
        repo = require_enterprise_repository(_TABLE)
        await _upsert_model_template_from_sync(
            repo, template
        )
        result = {
            "template_id": _normalize_template_id(template.get("template_id")),
        }
        logger.info(
            "[ManagerConfigReceiver] model_templates create template_id=%s",
            result["template_id"],
        )
        return result

    async def update(
        self,
        template_id: str,
        updates: dict[str, Any],
    ) -> None:
        if template_id is None:
            raise ValueError("model_templates.update requires template_id")
        if not isinstance(updates, dict) or not updates:
            raise ValueError("model_templates.update requires non-empty updates")
        req = ModelTemplateUpdateRequest.model_validate(updates)
        tid = _normalize_template_id(template_id)
        repo = require_enterprise_repository(_TABLE)
        row = await update_model_template(repo, tid, req)
        if row is None:
            raise ValueError(f"model template template_id={tid!r} not found")
        logger.info(
            "[ManagerConfigReceiver] model_templates update template_id=%s",
            tid,
        )

    async def delete(self, template_id: str) -> None:
        if template_id is None:
            raise ValueError("model_templates.delete requires template_id")
        tid = _normalize_template_id(template_id)
        repo = require_enterprise_repository(_TABLE)
        await delete_model_template(repo, tid)
        logger.info(
            "[ManagerConfigReceiver] model_templates delete template_id=%s",
            tid,
        )
