# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""扩展配置模板：将 Claw Manager 下发的 extension_config_templates 写入 Gateway 本地库。"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.gateway.config.enterprise.repository import EnterpriseRecordRepository
from jiuwenswarm.gateway.config.enterprise.tables.template_models import EXTENSION_CONFIG_TEMPLATE_TABLE_DEF

from ...infrastructure.repository_access import require_enterprise_repository
from ...infrastructure.utils import parse_iso_datetime, utc_now
from ...schemas.template_schemas import (
    ExtensionConfigTemplateUpdateRequest,
    HookConfig,
    normalize_hook_schedule,
)

_TABLE = EXTENSION_CONFIG_TEMPLATE_TABLE_DEF.table_name
_ALLOWED_COMPONENTS = frozenset({"gateway", "agent_server"})
_ALLOWED_HOOK_TYPES = frozenset({"pre_request", "post_request", "error", "schedule"})
logger = logging.getLogger(__name__)


def _normalize_template_id(template_id: Any) -> str:
    normalized = str(template_id or "").strip()
    if not normalized:
        raise ValueError("template_id is required")
    if len(normalized) > 100:
        raise ValueError("template_id must be at most 100 characters")
    return normalized


def _validate_component(value: str) -> str:
    normalized = value.strip()
    if normalized not in _ALLOWED_COMPONENTS:
        raise ValueError(
            f"component must be one of {sorted(_ALLOWED_COMPONENTS)}, got {value!r}"
        )
    return normalized


def _validate_hook_type(value: str) -> str:
    normalized = value.strip()
    if normalized not in _ALLOWED_HOOK_TYPES:
        raise ValueError(
            f"hook_type must be one of {sorted(_ALLOWED_HOOK_TYPES)}, got {value!r}"
        )
    return normalized


def _validate_hook_config(
    hook_config: HookConfig | dict[str, Any], *, hook_type: str
) -> dict[str, Any]:
    cfg = (
        hook_config
        if isinstance(hook_config, HookConfig)
        else HookConfig.model_validate(hook_config)
    )
    schedule = normalize_hook_schedule(
        cfg.schedule, required=(hook_type == "schedule")
    )
    data = cfg.model_dump(exclude_none=True)
    if schedule is None:
        data.pop("schedule", None)
    else:
        data["schedule"] = schedule
    return data


async def _get_row_for_instance(
    repo: EnterpriseRecordRepository,
    template_id: str,
) -> dict[str, Any] | None:
    return await repo.get(template_id=_normalize_template_id(template_id))


async def update_extension_config_template(
    repo: EnterpriseRecordRepository,
    template_id: str,
    request: ExtensionConfigTemplateUpdateRequest,
    *,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    tid = _normalize_template_id(template_id)
    row = existing if existing is not None else await _get_row_for_instance(repo, tid)
    if row is None:
        return None

    updates = request.model_dump(exclude_unset=True)
    hook_type = _validate_hook_type(
        str(updates.get("hook_type") or row.get("hook_type", ""))
    )
    if "hook_type" in updates and updates["hook_type"] is not None:
        updates["hook_type"] = hook_type
    if "component" in updates and updates["component"] is not None:
        updates["component"] = _validate_component(updates["component"])
    if "template_name" in updates and updates["template_name"] is not None:
        updates["template_name"] = updates["template_name"].strip()
    if "hook_config" in updates and updates["hook_config"] is not None:
        updates["hook_config"] = _validate_hook_config(
            updates["hook_config"], hook_type=hook_type
        )

    if not updates:
        raise ValueError("请求未包含任何可更新的业务字段")

    updates["updated_at"] = utc_now()
    updated = await repo.update({"template_id": tid}, updates)
    if updated is None:
        return None
    return {"template_id": str(updated.get("template_id", tid))}


async def delete_extension_config_template(
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
    component = _validate_component(str(template["component"]))
    hook_type = _validate_hook_type(str(template["hook_type"]))
    hook_config = _validate_hook_config(template["hook_config"], hook_type=hook_type)
    custom_config = template.get("custom_config")
    if custom_config is None:
        custom_config = {}
    return {
        "template_id": template_uuid,
        "template_name": str(template["template_name"]).strip(),
        "description": template.get("description"),
        "component": component,
        "hook_type": hook_type,
        "hook_config": hook_config,
        "custom_config": custom_config,
        "enabled": bool(template.get("enabled", True)),
        "data": template.get("data"),
        "created_at": now,
        "updated_at": now,
    }


async def _upsert_extension_config_template_from_sync(
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
        k: v for k, v in row_data.items() if k not in ("template_id",)
    }
    updates["updated_at"] = utc_now()
    await repo.update({"template_id": tid}, updates)


async def _sync_extension_config_templates_records(
    repo: EnterpriseRecordRepository,
    templates: list[dict[str, Any]]
) -> dict[str, Any]:
    incoming_ids: set[str] = set()
    synced = 0
    for item in templates:
        if not isinstance(item, dict):
            raise ValueError("extension_config_templates.sync templates must be objects")
        tid = _normalize_template_id(item.get("template_id"))
        incoming_ids.add(tid)
        await _upsert_extension_config_template_from_sync(
            repo, item
        )
        synced += 1
    deleted = 0
    for row in await repo.list():
        tid = str(row.get("template_id") or "")
        if tid and tid not in incoming_ids:
            if await delete_extension_config_template(repo, tid):
                deleted += 1
    return {"synced_count": synced, "deleted_count": deleted}


class ExtensionConfigTemplateService:

    async def create(
        self,
        template: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(template, dict):
            raise ValueError("extension_config_templates.create requires template object")
        repo = require_enterprise_repository(_TABLE)
        await _upsert_extension_config_template_from_sync(
            repo, template
        )
        result = {
            "template_id": _normalize_template_id(template.get("template_id")),
        }
        logger.info(
            "[ManagerConfigReceiver] extension_config_templates create template_id=%s",
            result["template_id"],
        )
        return result

    async def update(
        self,
        template_id: str,
        updates: dict[str, Any],
    ) -> None:
        if template_id is None:
            raise ValueError("extension_config_templates.update requires template_id")
        if not isinstance(updates, dict) or not updates:
            raise ValueError("extension_config_templates.update requires non-empty updates")
        req = ExtensionConfigTemplateUpdateRequest.model_validate(updates)
        tid = _normalize_template_id(template_id)
        repo = require_enterprise_repository(_TABLE)
        existing = await _get_row_for_instance(repo, tid)
        row = await update_extension_config_template(
            repo, tid, req, existing=existing
        )
        if row is None:
            raise ValueError(f"extension config template template_id={tid!r} not found")
        logger.info(
            "[ManagerConfigReceiver] extension_config_templates update template_id=%s",
            tid,
        )

    async def delete(self, template_id: str) -> None:
        if template_id is None:
            raise ValueError("extension_config_templates.delete requires template_id")
        tid = _normalize_template_id(template_id)
        repo = require_enterprise_repository(_TABLE)
        await delete_extension_config_template(repo, tid)
        logger.info(
            "[ManagerConfigReceiver] extension_config_templates delete template_id=%s",
            tid,
        )
