# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""定时任务面板个数上限：写入 Gateway 本地库 cron_policy。"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.gateway.config.enterprise.tables.application_config_models import (
    CRON_POLICY_TABLE_DEF,
)
from jiuwenswarm.gateway.cron.policy import coerce_max_jobs_per_user

from ...infrastructure.repository_access import require_enterprise_repository
from ...infrastructure.utils import format_ts, utc_now

_TABLE = CRON_POLICY_TABLE_DEF.table_name
logger = logging.getLogger(__name__)


def _require_limit(raw: Any) -> int:
    if raw is None or isinstance(raw, bool):
        raise ValueError("max_jobs_per_user must be an integer >= 0")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_jobs_per_user must be an integer >= 0") from exc
    if value < 0:
        raise ValueError("max_jobs_per_user must be an integer >= 0")
    return value


def _row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "max_jobs_per_user": coerce_max_jobs_per_user(row.get("max_jobs_per_user")),
        "created_at": format_ts(row.get("created_at")),
        "updated_at": format_ts(row.get("updated_at")),
    }


async def _upsert_cron_policy_record(request: dict[str, Any]) -> dict[str, Any]:
    repo = require_enterprise_repository(_TABLE)
    limit = _require_limit(request.get("max_jobs_per_user"))
    now = utc_now()
    existing = await repo.get()
    if existing is not None:
        updated = await repo.update({}, {"max_jobs_per_user": limit, "updated_at": now})
        if updated is None:
            raise ValueError("failed to update cron policy")
        return _row_to_dict(updated)
    created = await repo.create(
        {
            "max_jobs_per_user": limit,
            "created_at": now,
            "updated_at": now,
        }
    )
    return _row_to_dict(created)


class CronPolicyService:

    async def upsert(
        self,
        request: dict[str, Any] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        payload = dict(request or {})
        payload.update(fields)
        if isinstance(payload.get("cron_policy"), dict):
            payload = dict(payload["cron_policy"])
        return await _upsert_cron_policy_record(payload)

    async def delete(self) -> None:
        repo = require_enterprise_repository(_TABLE)
        await repo.delete()
        logger.info("[ManagerConfigReceiver] cron_policy deleted")
