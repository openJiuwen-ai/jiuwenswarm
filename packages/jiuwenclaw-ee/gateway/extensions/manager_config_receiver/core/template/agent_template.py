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


# agent 模板的变更要不要通知 runtime 重建，取决于变更内容：
# extension_config 槽的引用变化不需要——agentserver 读取该表带缓存过期，
# 稍后自然读到新值；其余槽位引用、启停、data 内容变化会影响新建 agent 的
# 配置，需要通知。
_EXTENSION_CONFIG_SLOT = "extension_config"
# 展示用元数据，不影响 agent 配置，变化无需通知。
_METADATA_FIELDS = frozenset({"template_name", "description", "agent_tags"})


def _sorted_refs(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(str(item) for item in value)


def _slot_refs_differ(old_refs: Any, new_refs: Any) -> bool:
    """比较各槽位引用的差异，跳过 extension_config 槽（列表顺序无关）。"""
    old = old_refs if isinstance(old_refs, dict) else {}
    new = new_refs if isinstance(new_refs, dict) else {}
    for slot in set(old) | set(new):
        if slot == _EXTENSION_CONFIG_SLOT:
            continue
        if _sorted_refs(old.get(slot)) != _sorted_refs(new.get(slot)):
            return True
    return False


def _runtime_relevant_change_for_upsert(
    existing: dict[str, Any] | None,
    template: dict[str, Any],
) -> bool:
    """整行写入是否影响 runtime 配置：新建、其他槽位引用或内容变化 → True。"""
    if existing is None:
        return True
    new_row = _build_row_from_template(template, now=None)
    if _slot_refs_differ(existing.get("template_ref"), new_row.get("template_ref")):
        return True
    return any(new_row.get(key) != existing.get(key) for key in ("enabled", "data"))


def _runtime_relevant_change_for_update(
    existing: dict[str, Any],
    updates: dict[str, Any],
) -> bool:
    """PATCH 是否影响 runtime 配置：只改 extension_config 引用或元数据 → False。"""
    for key, new_value in updates.items():
        if key in _METADATA_FIELDS or key in ("created_at", "updated_at"):
            continue
        if key == "template_ref":
            if _slot_refs_differ(existing.get("template_ref"), new_value):
                return True
            continue
        if new_value != existing.get(key):
            return True
    return False


async def _upsert_agent_template_from_sync(
    repo: EnterpriseRecordRepository,
    template: dict[str, Any],
    *,
    existing: dict[str, Any] | None,
) -> None:
    now = utc_now()
    tid = _normalize_template_id(template.get("template_id"))
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
) -> tuple[bool, dict[str, Any] | None]:
    """写入模板并返回 (变更是否影响 runtime 配置, 业务结果行)。"""
    tid = _normalize_template_id(template_id)
    row = existing if existing is not None else await _get_row_for_instance(repo, tid)
    if row is None:
        return False, None

    updates = request.model_dump(exclude_unset=True)
    if "template_name" in updates and updates["template_name"] is not None:
        updates["template_name"] = updates["template_name"].strip()
    if "template_ref" in updates and updates["template_ref"] is not None:
        updates["template_ref"] = normalize_template_ref(updates["template_ref"])
    if not updates:
        raise ValueError("请求未包含任何可更新的业务字段")

    runtime_relevant = _runtime_relevant_change_for_update(row, updates)
    updates["updated_at"] = utc_now()
    updated = await repo.update({"template_id": tid}, updates)
    if updated is None:
        return False, None
    return runtime_relevant, {"template_id": str(updated.get("template_id", tid))}


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
    ) -> tuple[dict[str, Any], bool]:
        """写入模板并返回 (业务结果, 变更是否影响 runtime 配置)。"""
        if not isinstance(template, dict):
            raise ValueError("agent_templates.create requires template object")
        repo = require_enterprise_repository(_TABLE)
        tid = _normalize_template_id(template.get("template_id"))
        existing = await _get_row_for_instance(repo, tid)
        runtime_relevant = _runtime_relevant_change_for_upsert(existing, template)
        await _upsert_agent_template_from_sync(
            repo, template, existing=existing
        )
        result = {"template_id": tid}
        logger.info(
            "[ManagerConfigReceiver] agent_templates create template_id=%s runtime_relevant=%s",
            tid,
            runtime_relevant,
        )
        return result, runtime_relevant

    async def update(
        self,
        template_id: str,
        updates: dict[str, Any],
    ) -> bool:
        """写入模板并返回变更是否影响 runtime 配置。"""
        if template_id is None:
            raise ValueError("agent_templates.update requires template_id")
        if not isinstance(updates, dict) or not updates:
            raise ValueError("agent_templates.update requires non-empty updates")
        req = AgentTemplateUpdateRequest.model_validate(updates)
        tid = _normalize_template_id(template_id)
        repo = require_enterprise_repository(_TABLE)
        runtime_relevant, row = await update_agent_template(repo, tid, req)
        if row is None:
            raise ValueError(f"agent template template_id={tid!r} not found")
        logger.info(
            "[ManagerConfigReceiver] agent_templates update template_id=%s runtime_relevant=%s",
            tid,
            runtime_relevant,
        )
        return runtime_relevant

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
