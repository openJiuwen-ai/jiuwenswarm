# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""专家包允许名单：写入 Gateway 本地库。出站请求从这一行读取。"""

from __future__ import annotations

import logging
from typing import Any

from gateway.config.enterprise.tables.application_config_models import (
    PACKAGE_ALLOWLIST_TABLE_DEF,
)
from jiuwenswarm.server.runtime.package_allowlist import (
    ensure_packages_fit,
    normalize_packages,
)

from ...infrastructure.repository_access import require_enterprise_repository
from ...infrastructure.utils import format_ts, utc_now

_TABLE = PACKAGE_ALLOWLIST_TABLE_DEF.table_name
logger = logging.getLogger(__name__)


async def read_package_allowlist_packages() -> list[dict[str, Any]]:
    """企业版读出当前行。没有行、读失败或内容非法时为空列表。个人版不读库。"""
    from gateway.enterprise import is_enterprise

    if not is_enterprise():
        return []
    try:
        repo = require_enterprise_repository(_TABLE)
        row = await repo.get()
    except Exception:
        logger.warning("[package_allowlist] read failed, sending empty", exc_info=True)
        return []
    if not isinstance(row, dict):
        return []
    try:
        packages = normalize_packages(row.get("packages"))
        ensure_packages_fit(packages)
    except ValueError:
        logger.warning("[package_allowlist] stored packages rejected, sending empty")
        return []
    return packages


def _row_to_dict(row: dict[str, Any], packages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "packages": packages,
        "created_at": format_ts(row.get("created_at")),
        "updated_at": format_ts(row.get("updated_at")),
    }


class PackageAllowlistService:

    async def upsert(
        self,
        request: dict[str, Any] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        payload = dict(request or {})
        payload.update(fields)
        if isinstance(payload.get("package_allowlist"), dict):
            payload = dict(payload["package_allowlist"])
        packages = normalize_packages(payload.get("packages"))
        ensure_packages_fit(packages)
        repo = require_enterprise_repository(_TABLE)
        now = utc_now()
        existing = await repo.get()
        if existing is not None:
            updated = await repo.update({}, {"packages": packages, "updated_at": now})
            if updated is None:
                raise ValueError("failed to update package allowlist")
            stored = updated
        else:
            stored = await repo.create(
                {"packages": packages, "created_at": now, "updated_at": now}
            )
            if stored is None:
                raise ValueError("failed to create package allowlist")
        logger.info("[ManagerConfigReceiver] package_allowlist synced count=%s", len(packages))
        return _row_to_dict(stored, packages)

    async def delete(self) -> None:
        repo = require_enterprise_repository(_TABLE)
        await repo.delete()
        logger.info("[ManagerConfigReceiver] package_allowlist deleted")
