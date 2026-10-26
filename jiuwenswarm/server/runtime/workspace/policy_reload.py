# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""从 Gateway 库刷新 workspace_quota_policy 进程缓存。"""

from __future__ import annotations

import logging
import time
from typing import Any

from jiuwenswarm.common.workspace.quota import (
    db_policies_loaded,
    db_policy_cache_loaded_at,
    get_db_policy_cache,
    set_db_policy_cache,
)
from jiuwenswarm.edition import is_enterprise

logger = logging.getLogger(__name__)

_DB_TTL_SECONDS = 2.0


def _db_policy_cache_fresh(*, force: bool) -> bool:
    if force or not db_policies_loaded():
        return False
    loaded_at = db_policy_cache_loaded_at()
    if loaded_at is None:
        return False
    return (time.monotonic() - loaded_at) < _DB_TTL_SECONDS


async def reload_quota_policies_from_gateway_db(*, force: bool = False) -> list[dict[str, Any]]:
    if not is_enterprise():
        return get_db_policy_cache()
    if _db_policy_cache_fresh(force=force):
        return get_db_policy_cache()
    try:
        from jiuwenswarm.server.runtime.enterprise_config import db_queries

        rows = await db_queries.list_records(
            "workspace_quota_policy",
            filters={"enabled": True},
            order_by="priority",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[workspace.quota] read workspace_quota_policy failed: %s", exc)
        return get_db_policy_cache()
    enabled = [
        row for row in rows if isinstance(row, dict) and bool(row.get("enabled", True))
    ]
    set_db_policy_cache(enabled)
    return get_db_policy_cache()
