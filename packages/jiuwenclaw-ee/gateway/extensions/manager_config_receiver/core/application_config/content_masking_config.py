# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""内容脱敏配置：管理端下发整段配置，写入 Gateway 本地库并热更新网页出站脱敏。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jiuwenswarm.gateway.config.enterprise.repository import EnterpriseRecordRepository
from jiuwenswarm.gateway.config.enterprise.tables.application_config_models import (
    CONTENT_MASKING_CONFIG_TABLE_DEF,
)

from ...infrastructure.repository_access import require_enterprise_repository
from ...infrastructure.utils import utc_now

_TABLE = CONTENT_MASKING_CONFIG_TABLE_DEF.table_name
_LIST_CAP = 50
logger = logging.getLogger(__name__)


def _validated_rules(rules: list[Any]) -> list[dict[str, str]]:
    from jiuwenswarm.gateway.channel_manager.web.sensitive_mask import (
        validate_sensitive_mask_rules,
    )

    try:
        # 管理端下发额外拒绝嵌套量词 ReDoS；个人版校验保持原样。
        return validate_sensitive_mask_rules(rules, strict=True)
    except TypeError as exc:
        raise ValueError(str(exc)) from exc


def _apply(row: dict[str, Any] | None) -> None:
    from jiuwenswarm.gateway.channel_manager.web.sensitive_mask import (
        apply_content_masking_row,
    )

    apply_content_masking_row(dict(row) if isinstance(row, dict) else None)


def _select_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    from jiuwenswarm.gateway.channel_manager.web.sensitive_mask import (
        select_content_masking_row,
    )

    return select_content_masking_row(rows)


async def _all_rows(repo: EnterpriseRecordRepository) -> list[dict[str, Any]]:
    return await repo.list(limit=_LIST_CAP)


_WRITE_LOCK = asyncio.Lock()


class ContentMaskingConfigService:

    async def upsert(self, *, enabled: bool, rules: list[Any]) -> dict[str, Any]:
        """PUT：整段替换；无行则创建，有行则递增 revision 并清理多余行。"""
        cleaned = _validated_rules(rules)
        # 单进程内串行化读改写，避免并发 PUT 建出两行或丢 revision。
        async with _WRITE_LOCK:
            return await self._upsert_locked(enabled, cleaned)

    async def _upsert_locked(
        self,
        enabled: bool,
        cleaned: list[dict[str, str]],
    ) -> dict[str, Any]:
        repo = require_enterprise_repository(_TABLE)
        now = utc_now()
        rows = await _all_rows(repo)
        existing = _select_row(rows)
        payload: dict[str, Any] = {
            "enabled": bool(enabled),
            "rules": cleaned,
            "source": "manager",
        }
        if existing is None:
            saved = await repo.create(
                {
                    **payload,
                    "revision": 1,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        else:
            saved = await repo.update_by_row_id(
                int(existing["id"]),
                {
                    **payload,
                    "revision": int(existing.get("revision") or 1) + 1,
                    "updated_at": now,
                },
            )
            if saved is None:
                raise ValueError("failed to update content masking config")
        # 历史多行（并发/异常残留）在这里收敛为单行，避免 DELETE 删不干净。
        saved_id = int(saved["id"]) if saved.get("id") is not None else None
        for row in rows:
            row_id = row.get("id")
            if row_id is None:
                continue
            if saved_id is not None and int(row_id) == saved_id:
                continue
            await repo.delete_by_row_id(int(row_id))
        _apply(saved)
        result = {
            "enabled": bool(saved.get("enabled")),
            "rules": list(saved.get("rules") or []),
            "revision": int(saved.get("revision") or 1),
        }
        logger.info(
            "[ManagerConfigReceiver] content_masking_config upsert revision=%s enabled=%s rules=%d",
            result["revision"],
            result["enabled"],
            len(result["rules"]),
        )
        return result

    async def delete(self) -> None:
        """DELETE：删除全部下发配置行，网页脱敏回退 config.yaml。"""
        async with _WRITE_LOCK:
            repo = require_enterprise_repository(_TABLE)
            rows = await _all_rows(repo)
            for row in rows:
                row_id = row.get("id")
                if row_id is None:
                    continue
                await repo.delete_by_row_id(int(row_id))
            _apply(None)
            logger.info(
                "[ManagerConfigReceiver] content_masking_config deleted rows=%d",
                len(rows),
            )


__all__ = ["ContentMaskingConfigService"]
