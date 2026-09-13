# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""预置技能模板：Manager 下发的 skill_prebuilt 写入 Gateway 本地库。"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from jiuwenswarm.gateway.config.enterprise.repository import EnterpriseRecordRepository
from jiuwenswarm.gateway.config.enterprise.tables.template_models import SKILL_PREBUILT_TEMPLATE_TABLE_DEF

from ...infrastructure.repository_access import require_enterprise_repository
from ...infrastructure.utils import parse_iso_datetime, utc_now
from ...schemas.template_schemas import SkillPrebuiltTemplateUpdateRequest

_TABLE = SKILL_PREBUILT_TEMPLATE_TABLE_DEF.table_name
logger = logging.getLogger(__name__)

_SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DATA_FORBIDDEN_KEYS = frozenset({"source_id", "skill_id", "version_id", "package_url"})


def _normalize_template_id(template_id: Any) -> str:
    normalized = str(template_id or "").strip()
    if not normalized:
        raise ValueError("template_id is required")
    if len(normalized) > 100:
        raise ValueError("template_id must be at most 100 characters")
    return normalized


def _normalize_skill_id(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("skill_id is required")
    if len(normalized) > 512:
        raise ValueError("skill_id must be at most 512 characters")
    return normalized


def _normalize_optional_str(value: Any, *, max_len: int, field: str) -> str:
    normalized = str(value or "").strip()
    if len(normalized) > max_len:
        raise ValueError(f"{field} must be at most {max_len} characters")
    return normalized


def _validate_http_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            "invalid_template: url path requires http(s) package_url"
        )
    return value


def _resolve_package_url(payload: dict[str, Any]) -> str:
    return str(payload.get("package_url") or "").strip()


def _normalize_data(data: Any) -> dict[str, Any] | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError("data must be an object")
    forbidden = _DATA_FORBIDDEN_KEYS.intersection(data.keys())
    if forbidden:
        raise ValueError(
            f"invalid_template: data must not contain {sorted(forbidden)}; use top-level fields"
        )
    sha = data.get("sha256")
    if sha is not None:
        sha_s = str(sha).strip().lower()
        if sha_s and not _SHA256_RE.fullmatch(sha_s):
            raise ValueError("invalid_template: data.sha256 must be 64 lowercase hex")
        data = dict(data)
        data["sha256"] = sha_s
    return data


def _assert_install_path(
    *,
    skill_id: str,
    package_url: str,
    source_id: str,
    version_id: str,
) -> None:
    """合并后必须能推断 provider 或 url（provider 优先）。"""
    if not skill_id:
        raise ValueError("skill_id is required")
    if source_id and version_id:
        if not _SOURCE_ID_RE.fullmatch(source_id):
            raise ValueError(
                "invalid_template: provider path requires source_id and version_id"
            )
        return
    if source_id or version_id:
        raise ValueError(
            "invalid_template: provider path requires source_id and version_id"
        )
    if package_url:
        _validate_http_url(package_url)
        return
    raise ValueError(
        "invalid_template: cannot infer install path from fields"
    )


def _normalize_row_fields(payload: dict[str, Any]) -> dict[str, Any]:
    skill_id = _normalize_skill_id(str(payload.get("skill_id") or ""))
    package_url = _resolve_package_url(payload)
    if package_url:
        package_url = _normalize_optional_str(
            package_url, max_len=2048, field="package_url"
        )
        package_url = _validate_http_url(package_url) if package_url else ""
    source_id = _normalize_optional_str(
        payload.get("source_id"), max_len=64, field="source_id"
    )
    version_id = _normalize_optional_str(
        payload.get("version_id"), max_len=128, field="version_id"
    )
    data = _normalize_data(payload.get("data"))
    _assert_install_path(
        skill_id=skill_id,
        package_url=package_url,
        source_id=source_id,
        version_id=version_id,
    )
    return {
        "skill_id": skill_id,
        "package_url": package_url or None,
        "source_id": source_id or None,
        "version_id": version_id or None,
        "data": data,
    }


async def _get_row_for_instance(
    repo: EnterpriseRecordRepository,
    template_id: str,
) -> dict[str, Any] | None:
    return await repo.get(template_id=_normalize_template_id(template_id))


async def update_skill_prebuilt_template(
    repo: EnterpriseRecordRepository,
    template_id: str,
    request: SkillPrebuiltTemplateUpdateRequest,
    *,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    tid = _normalize_template_id(template_id)
    row = existing if existing is not None else await _get_row_for_instance(repo, tid)
    if row is None:
        return None

    updates = request.model_dump(exclude_unset=True)
    if not updates:
        raise ValueError("请求未包含任何可更新的业务字段")

    if "template_name" in updates and updates["template_name"] is not None:
        updates["template_name"] = updates["template_name"].strip()

    # 与库内行合并后再做安装路径校验
    merged = dict(row)
    merged.update(updates)
    normalized = _normalize_row_fields(merged)
    updates.update({
        "skill_id": normalized["skill_id"],
        "package_url": normalized["package_url"],
        "source_id": normalized["source_id"],
        "version_id": normalized["version_id"],
    })
    if "data" in request.model_dump(exclude_unset=True):
        updates["data"] = normalized["data"]
    updates["updated_at"] = utc_now()
    updated = await repo.update({"template_id": tid}, updates)
    if updated is None:
        return None
    return {"template_id": str(updated.get("template_id", tid))}


async def delete_skill_prebuilt_template(
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
    fields = _normalize_row_fields(template)
    return {
        "template_id": template_uuid,
        "template_name": str(template["template_name"]).strip(),
        "description": template.get("description"),
        "skill_id": fields["skill_id"],
        "package_url": fields["package_url"],
        "source_id": fields["source_id"],
        "version_id": fields["version_id"],
        "enabled": bool(template.get("enabled", True)),
        "data": fields["data"],
        "created_at": now,
        "updated_at": now,
    }


async def _upsert_skill_prebuilt_template_from_sync(
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
        row_data["created_at"] = parse_iso_datetime(created_at) or now
    updates = {
        k: v for k, v in row_data.items() if k not in ("template_id",)
    }
    updates["updated_at"] = utc_now()
    await repo.update({"template_id": tid}, updates)


async def _sync_skill_prebuilt_templates_records(
    repo: EnterpriseRecordRepository,
    templates: list[dict[str, Any]]
) -> dict[str, Any]:
    incoming_ids: set[str] = set()
    synced = 0
    for item in templates:
        if not isinstance(item, dict):
            raise ValueError("skill_prebuilt_templates.sync templates must be objects")
        tid = _normalize_template_id(item.get("template_id"))
        incoming_ids.add(tid)
        await _upsert_skill_prebuilt_template_from_sync(
            repo, item
        )
        synced += 1
    deleted = 0
    for row in await repo.list():
        tid = str(row.get("template_id") or "")
        if tid and tid not in incoming_ids:
            if await delete_skill_prebuilt_template(repo, tid):
                deleted += 1
    return {"synced_count": synced, "deleted_count": deleted}


class SkillPrebuiltTemplateService:

    async def create(
        self,
        template: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(template, dict):
            raise ValueError("skill_prebuilt_templates.create requires template object")
        repo = require_enterprise_repository(_TABLE)
        await _upsert_skill_prebuilt_template_from_sync(
            repo, template
        )
        result = {
            "template_id": _normalize_template_id(template.get("template_id")),
        }
        logger.info(
            "[ManagerConfigReceiver] skill_prebuilt_templates create template_id=%s",
            result["template_id"],
        )
        return result

    async def update(
        self,
        template_id: str,
        updates: dict[str, Any],
    ) -> None:
        if template_id is None:
            raise ValueError("skill_prebuilt_templates.update requires template_id")
        if not isinstance(updates, dict) or not updates:
            raise ValueError("skill_prebuilt_templates.update requires non-empty updates")
        req = SkillPrebuiltTemplateUpdateRequest.model_validate(updates)
        tid = _normalize_template_id(template_id)
        repo = require_enterprise_repository(_TABLE)
        existing = await _get_row_for_instance(repo, tid)
        row = await update_skill_prebuilt_template(
            repo, tid, req, existing=existing
        )
        if row is None:
            raise ValueError(f"skill prebuilt template template_id={tid!r} not found")
        logger.info(
            "[ManagerConfigReceiver] skill_prebuilt_templates update template_id=%s",
            tid,
        )

    async def delete(self, template_id: str) -> None:
        if template_id is None:
            raise ValueError("skill_prebuilt_templates.delete requires template_id")
        tid = _normalize_template_id(template_id)
        repo = require_enterprise_repository(_TABLE)
        await delete_skill_prebuilt_template(repo, tid)
        logger.info(
            "[ManagerConfigReceiver] skill_prebuilt_templates delete template_id=%s",
            tid,
        )

