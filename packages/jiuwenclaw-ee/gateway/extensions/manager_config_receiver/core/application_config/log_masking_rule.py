# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""日志脱敏规则：将 Claw Manager 下发的 log_masking_rule 写入 Gateway 本地库。"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.gateway.config.enterprise.repository import EnterpriseRecordRepository
from jiuwenswarm.gateway.config.enterprise.tables.application_config_models import LOG_MASKING_RULE_TABLE_DEF
from jiuwenswarm.infrastructure.log_masking.engine import LogMaskingEngine
from ...infrastructure.repository_access import require_enterprise_repository
from ...infrastructure.utils import format_ts, parse_iso_datetime, utc_now
from ...schemas.application_config_schemas import (
    LogMaskingRuleCreateRequest,
    LogMaskingRuleUpdateRequest,
)

_TABLE = LOG_MASKING_RULE_TABLE_DEF.table_name
logger = logging.getLogger(__name__)


def _rule_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "rule_id": row.get("rule_id"),
        "rule_name": row.get("rule_name"),
        "description": row.get("description"),
        "pattern": row.get("pattern"),
        "replacement": row.get("replacement"),
        "priority": row.get("priority", 0),
        "with_fingerprint": bool(row.get("with_fingerprint", False)),
        "source": row.get("source"),
        "enabled": bool(row.get("enabled", True)),
        "data": row.get("data"),
        "created_at": format_ts(row.get("created_at")),
        "updated_at": format_ts(row.get("updated_at")),
    }


async def _upsert_log_masking_rule_from_sync(
    repo: EnterpriseRecordRepository,
    request: LogMaskingRuleCreateRequest,
) -> dict[str, Any]:
    """按 ``rule_id`` upsert（对齐 agent_template；全量同步 POST 幂等）。"""
    from jiuwenswarm.infrastructure.log_masking.engine import (
        normalize_replacement,
        normalize_rule_id,
        normalize_source,
        validate_pattern,
    )

    rule_id = normalize_rule_id(request.rule_id)
    source = normalize_source(request.source)
    now = utc_now()
    row_data: dict[str, Any] = {
        "rule_id": rule_id,
        "rule_name": request.rule_name,
        "description": request.description,
        "pattern": validate_pattern(
            request.pattern,
            check_structure=False,
            check_performance=False,
        ),
        "replacement": normalize_replacement(request.replacement),
        "priority": int(request.priority),
        "with_fingerprint": bool(request.with_fingerprint),
        "source": source,
        "enabled": bool(request.enabled),
        "data": request.data,
        "created_at": now,
        "updated_at": now,
    }

    existing = await repo.get(rule_id=rule_id)
    if existing is None:
        record = await repo.create(row_data)
        return _rule_row_to_dict(record)

    created_at = existing.get("created_at")
    if created_at is not None:
        # existing 可能是 ISO 字符串；asyncpg 要求 datetime
        row_data["created_at"] = parse_iso_datetime(created_at) or now
    updates = {
        key: value
        for key, value in row_data.items()
        if key not in ("rule_id",)
    }
    updates["updated_at"] = utc_now()
    updated = await repo.update({"rule_id": rule_id}, updates)
    if updated is None:
        raise ValueError(f"failed to upsert log masking rule id={rule_id!r}")
    return _rule_row_to_dict(updated)


async def _update_log_masking_rule_record(
    repo: EnterpriseRecordRepository,
    rule_id: str,
    request: LogMaskingRuleUpdateRequest,
) -> dict[str, Any] | None:
    from jiuwenswarm.infrastructure.log_masking.engine import (
        normalize_replacement,
        normalize_rule_id,
        normalize_source,
        validate_pattern,
    )

    rid = normalize_rule_id(rule_id)
    existing = await repo.get(rule_id=rid)
    if existing is None:
        return None

    updates = request.model_dump(exclude_unset=True)
    if not updates:
        raise ValueError("updates must not be empty")

    if "pattern" in updates and updates["pattern"] is not None:
        updates["pattern"] = validate_pattern(
            updates["pattern"],
            check_structure=False,
            check_performance=False,
        )
    if "replacement" in updates:
        updates["replacement"] = normalize_replacement(updates.get("replacement"))
    if "source" in updates and updates["source"] is not None:
        updates["source"] = normalize_source(updates["source"])
    if "priority" in updates and updates["priority"] is not None:
        updates["priority"] = int(updates["priority"])
    if "with_fingerprint" in updates and updates["with_fingerprint"] is not None:
        updates["with_fingerprint"] = bool(updates["with_fingerprint"])

    updates["updated_at"] = utc_now()
    updated = await repo.update({"rule_id": rid}, updates)
    if updated is None:
        return None
    return _rule_row_to_dict(updated)


async def _delete_log_masking_rule_record(
    repo: EnterpriseRecordRepository,
    rule_id: str,
) -> bool:
    from jiuwenswarm.infrastructure.log_masking.engine import normalize_rule_id

    rid = normalize_rule_id(rule_id)
    return await repo.delete(rule_id=rid)


class LogMaskingRuleService:

    async def create(
        self,
        rule: dict[str, Any],
    ) -> dict[str, Any]:
        """POST create：按 ``rule_id`` upsert（全量同步幂等）。"""
        if not isinstance(rule, dict):
            raise ValueError("log_masking_rule.create requires rule object")
        req = LogMaskingRuleCreateRequest.model_validate(rule)
        repo = require_enterprise_repository(_TABLE)
        row = await _upsert_log_masking_rule_from_sync(repo, req)
        await LogMaskingEngine.reload_log_masking_rule(db_authoritative=True)
        result = {"rule_id": row["rule_id"]}
        logger.info(
            "[ManagerConfigReceiver] log_masking_rule upsert rule_id=%s",
            result["rule_id"],
        )
        return result

    async def update(
        self,
        rule_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        rid = str(rule_id or "").strip()
        if not rid:
            raise ValueError("log_masking_rule.update requires rule_id")
        if not isinstance(updates, dict) or not updates:
            raise ValueError("log_masking_rule.update requires non-empty updates")
        req = LogMaskingRuleUpdateRequest.model_validate(updates)
        repo = require_enterprise_repository(_TABLE)
        row = await _update_log_masking_rule_record(repo, rid, req)
        if row is None:
            raise ValueError(f"log masking rule id={rid!r} not found")
        await LogMaskingEngine.reload_log_masking_rule(db_authoritative=True)
        result = {"rule_id": row["rule_id"]}
        logger.info(
            "[ManagerConfigReceiver] log_masking_rule update rule_id=%s",
            result["rule_id"],
        )
        return result

    async def delete(self, rule_id: str) -> None:
        rid = str(rule_id or "").strip()
        if not rid:
            raise ValueError("log_masking_rule.delete requires rule_id")
        repo = require_enterprise_repository(_TABLE)
        deleted = await _delete_log_masking_rule_record(repo, rid)
        if not deleted:
            raise ValueError(f"log masking rule id={rid!r} not found")
        await LogMaskingEngine.reload_log_masking_rule(db_authoritative=True)
        logger.info(
            "[ManagerConfigReceiver] log_masking_rule delete rule_id=%s",
            rid,
        )
